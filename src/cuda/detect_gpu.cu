/*
 * detect_gpu.cu — CUDA implementation of the two detection stages.
 *
 * K1 (parallel pattern matcher): one GPU thread per log line. Patterns live
 *   in __constant__ memory; each thread scans its line with a naive search
 *   identical in semantics to the CPU reference.
 *
 * K2 (sliding-window statistics):
 *   pass 1 — one thread per line atomically inserts (ip, bucket, service)
 *            into an open-addressing device table (linear probing,
 *            atomicCAS claim + atomicAdd count).
 *   pass 2 — per-slot threads accumulate exact u64 sums (count, count^2).
 *   pass 3 — mean/std computed in double ON THE HOST (bit-identical to the
 *            CPU path), then one thread per slot flags z-score outliers.
 *   Flagged cells are sorted on the host by (bucket, service, ip) so both
 *   backends emit byte-identical anomaly lists.
 *
 * Chunked staging: input bytes are moved host->device through PINNED memory
 * in chunks (default 64 MB) on a dedicated stream with cudaMemcpyAsync;
 * chunk boundaries are always line boundaries.
 */
#include "tokenizer.h"

#include <cuda_runtime.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace {

constexpr uint64_t TABLE_SLOTS = 1u << 21; /* power of two, matches CPU */
constexpr uint32_t SENTINEL_MIN_CELLS = 32;
constexpr size_t PINNED_CHUNK_BYTES = (size_t)64 << 20; /* 64 MB staging */

#define CUDA_CHECK(call)                                                       \
    do {                                                                       \
        cudaError_t err_ = (call);                                             \
        if (err_ != cudaSuccess) {                                             \
            fprintf(stderr, "CUDA error %s at %s:%d: %s\n", #call, __FILE__,   \
                    __LINE__, cudaGetErrorString(err_));                       \
            return -3;                                                          \
        }                                                                      \
    } while (0)

/* ----------------------------- K1 ----------------------------- */

__constant__ char k_patterns[SENTINEL_MAX_PATTERNS][SENTINEL_MAX_PATTERN_LEN + 1];
__constant__ uint32_t k_pattern_lens[SENTINEL_MAX_PATTERNS];
__constant__ uint32_t k_pattern_count;

__device__ __forceinline__ void k1_scan_line(const uint8_t* line, size_t llen,
                                             uint32_t* flags_out) {
    uint32_t flags = 0;
    for (uint32_t p = 0; p < k_pattern_count; ++p) {
        uint32_t plen = k_pattern_lens[p];
        if (plen == 0 || llen < plen) continue;
        const char* pat = k_patterns[p];
        bool hit = false;
        for (size_t i = 0; i + plen <= llen && !hit; ++i) {
            size_t j = 0;
            while (j < plen && line[i + j] == (uint8_t)pat[j]) ++j;
            hit = (j == plen);
        }
        if (hit) flags |= (1u << p);
    }
    *flags_out = flags;
}

__global__ void k1_matcher_kernel(const uint8_t* __restrict__ chunk,
                                  const uint32_t* __restrict__ offsets,
                                  uint32_t line_begin, uint32_t line_end,
                                  uint32_t* __restrict__ line_flags) {
    /* one GPU thread per log line, grid-stride for occupancy */
    uint32_t n = line_end - line_begin;
    for (uint32_t idx = blockIdx.x * blockDim.x + threadIdx.x; idx < n;
         idx += gridDim.x * blockDim.x) {
        uint32_t li = line_begin + idx;
        const uint8_t* line = chunk + offsets[idx];
        size_t llen = offsets[idx + 1] - offsets[idx];
        k1_scan_line(line, llen, &line_flags[li]);
    }
}

/* ----------------------------- K2 ----------------------------- */

struct CellTable {
    uint64_t* keys;   /* biased key; 0 = empty */
    uint32_t* counts;
};

__device__ __forceinline__ uint64_t cell_hash(uint64_t key) {
    return key * 0x9E3779B97F4A7C15ull;
}

__device__ __forceinline__ void table_add(CellTable t, uint64_t skey,
                                          uint32_t count) {
    uint32_t mask = (uint32_t)(TABLE_SLOTS - 1);
    uint32_t s = (uint32_t)((cell_hash(skey) >> 32) & mask);
    for (;;) {
        uint64_t prev = atomicCAS(reinterpret_cast<unsigned long long*>(&t.keys[s]),
                                  0ull, static_cast<unsigned long long>(skey));
        if (prev == 0 || prev == skey) {
            atomicAdd(&t.counts[s], count);
            return;
        }
        s = (s + 1) & mask;
    }
}

/* pass 1: per-cell counts for one chunk of lines */
__global__ void k2_count_kernel(const uint32_t* __restrict__ line_ip,
                                const uint16_t* __restrict__ line_bucket,
                                const uint8_t* __restrict__ line_service,
                                uint32_t line_begin, uint32_t line_end,
                                CellTable table) {
    uint32_t n = line_end - line_begin;
    for (uint32_t idx = blockIdx.x * blockDim.x + threadIdx.x; idx < n;
         idx += gridDim.x * blockDim.x) {
        uint32_t li = line_begin + idx;
        uint64_t key = sentinel_cell_key(line_ip[li], line_bucket[li],
                                         line_service[li]);
        table_add(table, key, 1);
    }
}

/* pass 2: exact global statistics */
__global__ void k2_stats_kernel(CellTable table, unsigned long long* sum,
                                unsigned long long* sumsq,
                                unsigned long long* ncells) {
    uint32_t mask = (uint32_t)(TABLE_SLOTS - 1);
    for (uint32_t s = blockIdx.x * blockDim.x + threadIdx.x; s < TABLE_SLOTS;
         s += gridDim.x * blockDim.x) {
        uint32_t c = table.counts[s];
        if (c) {
            atomicAdd(sum, (unsigned long long)c);
            atomicAdd(sumsq, (unsigned long long)c * c);
            atomicAdd(ncells, 1ull);
        }
    }
}

/* pass 3: z-score flagging; mean/std arrive from host (double math) */
__global__ void k2_flag_kernel(CellTable table, double mean, double std,
                               float threshold, uint32_t* out_records,
                               uint32_t* out_count) {
    for (uint32_t s = blockIdx.x * blockDim.x + threadIdx.x; s < TABLE_SLOTS;
         s += gridDim.x * blockDim.x) {
        uint32_t c = table.counts[s];
        if (!c) continue;
        double z = (std > 0.0) ? ((double)c - mean) / std : 0.0;
        if (z >= (double)threshold) {
            uint32_t idx = atomicAdd(out_count, 1u);
            uint64_t key = table.keys[s] & ~SENTINEL_KEY_BIAS;
            out_records[4 * idx + 0] = sentinel_key_ip(key);
            out_records[4 * idx + 1] = sentinel_key_bucket(key);
            out_records[4 * idx + 2] = sentinel_key_service(key);
            out_records[4 * idx + 3] = c;
        }
    }
}

struct AnomRec {
    uint16_t bucket;
    uint8_t service;
    uint32_t ip;
    uint32_t count;
};

static bool anom_less(const AnomRec& a, const AnomRec& b) {
    if (a.bucket != b.bucket) return a.bucket < b.bucket;
    if (a.service != b.service) return a.service < b.service;
    return a.ip < b.ip;
}

/* Repack raw 4-word records into AnomRec, sort, emit back as 4-word records
 * (bucket, service, ip order) — identical to the CPU path's output order. */
static uint32_t* sort_records(uint32_t* raw, uint32_t count) {
    AnomRec* hits = (AnomRec*)malloc(sizeof(AnomRec) * count);
    if (!hits) return nullptr;
    for (uint32_t i = 0; i < count; ++i) {
        hits[i].ip = raw[4 * i + 0];
        hits[i].bucket = (uint16_t)raw[4 * i + 1];
        hits[i].service = (uint8_t)raw[4 * i + 2];
        hits[i].count = raw[4 * i + 3];
    }
    std::sort(hits, hits + count, anom_less);
    free(raw);
    uint32_t* out = (uint32_t*)malloc(sizeof(uint32_t) * 4 * count);
    if (!out) { free(hits); return nullptr; }
    for (uint32_t i = 0; i < count; ++i) {
        out[4 * i + 0] = hits[i].ip;
        out[4 * i + 1] = hits[i].bucket;
        out[4 * i + 2] = hits[i].service;
        out[4 * i + 3] = hits[i].count;
    }
    free(hits);
    return out;
}

} // namespace

extern "C" int sentinel_detect_gpu(const sentinel_input* in,
                                   const sentinel_config* cfg,
                                   sentinel_results* out) {
    if (!in || !cfg || !out || !in->data || !in->line_offsets || !in->line_ip ||
        !in->line_bucket || !in->line_service) {
        return -1;
    }
    const uint32_t n = in->line_count;
    memset(out, 0, sizeof(*out));

    /* ---- host-side setup ---- */
    if (cfg->pattern_count > SENTINEL_MAX_PATTERNS) return -4;
    char host_patterns[SENTINEL_MAX_PATTERNS][SENTINEL_MAX_PATTERN_LEN + 1] = {};
    uint32_t host_lens[SENTINEL_MAX_PATTERNS] = {};
    for (uint32_t p = 0; p < cfg->pattern_count; ++p) {
        size_t plen = strlen(cfg->patterns[p]);
        if (plen > SENTINEL_MAX_PATTERN_LEN) return -4;
        memcpy(host_patterns[p], cfg->patterns[p], plen);
        host_lens[p] = (uint32_t)plen;
    }

    out->line_flags = (uint32_t*)calloc(n ? n : 1, sizeof(uint32_t));
    if (!out->line_flags) return -2;

    /* ---- device allocations ---- */
    uint8_t* d_chunk = nullptr;
    uint32_t* d_offsets = nullptr;
    uint32_t* d_flags = nullptr;
    uint32_t* d_ip = nullptr;
    uint16_t* d_bucket = nullptr;
    uint8_t* d_service = nullptr;
    CellTable table{};
    uint32_t* d_records = nullptr;
    uint32_t* d_out_count = nullptr;
    unsigned long long* d_sum = nullptr;

    size_t chunk_cap = PINNED_CHUNK_BYTES + (1u << 20); /* + 1 MB line headroom */

    uint8_t* h_pinned = nullptr;
    cudaStream_t stream = nullptr;

    /* Scope guard via goto-cleanup pattern (C-style CUDA resource mgmt). */
    int rc = 0;
    do {
        CUDA_CHECK(cudaMalloc(&d_chunk, chunk_cap));
        CUDA_CHECK(cudaMalloc(&d_offsets, sizeof(uint32_t) * (n + 1)));
        CUDA_CHECK(cudaMalloc(&d_flags, sizeof(uint32_t) * (n ? n : 1)));
        CUDA_CHECK(cudaMalloc(&d_ip, sizeof(uint32_t) * (n ? n : 1)));
        CUDA_CHECK(cudaMalloc(&d_bucket, sizeof(uint16_t) * (n ? n : 1)));
        CUDA_CHECK(cudaMalloc(&d_service, sizeof(uint8_t) * (n ? n : 1)));
        CUDA_CHECK(cudaMalloc(&table.keys, sizeof(uint64_t) * TABLE_SLOTS));
        CUDA_CHECK(cudaMalloc(&table.counts, sizeof(uint32_t) * TABLE_SLOTS));
        CUDA_CHECK(cudaMalloc(&d_records,
                              sizeof(uint32_t) * 4 * TABLE_SLOTS)); /* worst case */
        CUDA_CHECK(cudaMalloc(&d_out_count, sizeof(uint32_t)));
        CUDA_CHECK(cudaMalloc(&d_sum, sizeof(unsigned long long) * 3));

        CUDA_CHECK(cudaHostAlloc(&h_pinned, chunk_cap, cudaHostAllocDefault));
        CUDA_CHECK(cudaStreamCreate(&stream));

        CUDA_CHECK(cudaMemcpyToSymbolAsync(k_patterns, host_patterns,
                                           sizeof(host_patterns), 0,
                                           cudaMemcpyHostToDevice, stream));
        CUDA_CHECK(cudaMemcpyToSymbolAsync(k_pattern_lens, host_lens,
                                           sizeof(host_lens), 0,
                                           cudaMemcpyHostToDevice, stream));
        CUDA_CHECK(cudaMemcpyToSymbolAsync(k_pattern_count, &cfg->pattern_count,
                                           sizeof(uint32_t), 0,
                                           cudaMemcpyHostToDevice, stream));

        CUDA_CHECK(cudaMemsetAsync(table.keys, 0,
                                   sizeof(uint64_t) * TABLE_SLOTS, stream));
        CUDA_CHECK(cudaMemsetAsync(table.counts, 0,
                                   sizeof(uint32_t) * TABLE_SLOTS, stream));
        CUDA_CHECK(cudaMemsetAsync(d_out_count, 0, sizeof(uint32_t), stream));
        CUDA_CHECK(cudaMemsetAsync(d_sum, 0, sizeof(unsigned long long) * 3, stream));

        CUDA_CHECK(cudaMemcpyAsync(d_ip, in->line_ip,
                                   sizeof(uint32_t) * n, cudaMemcpyHostToDevice,
                                   stream));
        CUDA_CHECK(cudaMemcpyAsync(d_bucket, in->line_bucket,
                                   sizeof(uint16_t) * n, cudaMemcpyHostToDevice,
                                   stream));
        CUDA_CHECK(cudaMemcpyAsync(d_service, in->line_service,
                                   sizeof(uint8_t) * n, cudaMemcpyHostToDevice,
                                   stream));

        const int k1_block = 256, k2_block = 256;
        const int k1_grid = (int)std::min<uint64_t>(2048, (n + k1_block - 1) / k1_block + 1);
        const int k2_grid = 1024;

        /* ---- chunked staging over line-aligned byte ranges ---- */
        uint32_t li = 0;
        while (li < n) {
            /* extend the chunk until byte budget is reached at a line edge */
            uint32_t first = li;
            uint32_t end = li;
            size_t bytes = 0;
            while (end < n) {
                size_t lb = (size_t)in->line_offsets[end + 1] - in->line_offsets[first];
                if (bytes && lb > PINNED_CHUNK_BYTES) break;
                bytes = lb;
                ++end;
            }

            size_t off = in->line_offsets[first];
            size_t len = in->line_offsets[end] - off;

            /* build per-chunk local offsets (line i -> its start within chunk) */
            uint32_t* local = (uint32_t*)malloc(sizeof(uint32_t) * (end - first + 1));
            if (!local) { rc = -2; break; }
            for (uint32_t k = 0; k <= end - first; ++k) {
                local[k] = in->line_offsets[first + k] - (uint32_t)off;
            }

            memcpy(h_pinned, in->data + off, len);
            CUDA_CHECK(cudaMemcpyAsync(d_chunk, h_pinned, len,
                                       cudaMemcpyHostToDevice, stream));
            CUDA_CHECK(cudaMemcpyAsync(d_offsets, local,
                                       sizeof(uint32_t) * (end - first + 1),
                                       cudaMemcpyHostToDevice, stream));
            free(local);
            if (rc) break;

            k1_matcher_kernel<<<k1_grid, k1_block, 0, stream>>>(
                d_chunk, d_offsets, first, end, d_flags);
            k2_count_kernel<<<k2_grid, k2_block, 0, stream>>>(
                d_ip, d_bucket, d_service, first, end, table);

            li = end;
        }
        if (rc) break;

        /* ---- K2 pass 2 + pass 3 over the whole table ---- */
        k2_stats_kernel<<<256, 256, 0, stream>>>(table, d_sum, d_sum + 1,
                                                 d_sum + 2);

        unsigned long long h_stats[3];
        CUDA_CHECK(cudaMemcpyAsync(h_stats, d_sum, sizeof(h_stats),
                                   cudaMemcpyDeviceToHost, stream));
        CUDA_CHECK(cudaStreamSynchronize(stream));

        double mean = 0.0, std = 0.0;
        if (h_stats[2]) {
            mean = (double)h_stats[0] / (double)h_stats[2];
            double var = (double)h_stats[1] / (double)h_stats[2] - mean * mean;
            std = var > 0.0 ? sqrt(var) : 0.0;
        }
        out->cells = h_stats[2];
        out->mean = mean;
        out->std = std;
        if (h_stats[2] >= SENTINEL_MIN_CELLS) {
            k2_flag_kernel<<<256, 256, 0, stream>>>(
                table, mean, std, cfg->z_threshold, d_records, d_out_count);
        }

        CUDA_CHECK(cudaMemcpyAsync(out->line_flags, d_flags,
                                   sizeof(uint32_t) * n, cudaMemcpyDeviceToHost,
                                   stream));

        uint32_t h_count = 0;
        CUDA_CHECK(cudaMemcpyAsync(&h_count, d_out_count, sizeof(uint32_t),
                                   cudaMemcpyDeviceToHost, stream));
        CUDA_CHECK(cudaStreamSynchronize(stream));

        if (h_count) {
            uint32_t* recs = (uint32_t*)malloc(sizeof(uint32_t) * 4 * h_count);
            if (!recs) { rc = -2; break; }
            CUDA_CHECK(cudaMemcpy(recs, d_records,
                                  sizeof(uint32_t) * 4 * h_count,
                                  cudaMemcpyDeviceToHost));
            uint32_t* sorted = sort_records(recs, h_count);
            if (!sorted) { free(recs); rc = -2; break; }
            out->anomaly_count = h_count;
            out->anomaly_records = sorted;
        }
    } while (0);

    /* ---- cleanup ---- */
    if (d_chunk) cudaFree(d_chunk);
    if (d_offsets) cudaFree(d_offsets);
    if (d_flags) cudaFree(d_flags);
    if (d_ip) cudaFree(d_ip);
    if (d_bucket) cudaFree(d_bucket);
    if (d_service) cudaFree(d_service);
    if (table.keys) cudaFree(table.keys);
    if (table.counts) cudaFree(table.counts);
    if (d_records) cudaFree(d_records);
    if (d_out_count) cudaFree(d_out_count);
    if (d_sum) cudaFree(d_sum);
    if (h_pinned) cudaFreeHost(h_pinned);
    if (stream) cudaStreamDestroy(stream);

    if (rc) {
        sentinel_free_results(out);
        return rc;
    }
    return 0;
}
