/*
 * detect_cpu.cpp — sequential reference implementation of the two detection
 * stages. Semantics must match detect_gpu.cu exactly:
 *
 *   K1: for each line, test each pattern (naive substring scan); set bit i
 *       in line_flags[line] when pattern i matches.
 *   K2: pass 1 — histogram of lines per (ip, bucket, service) cell in an
 *       open-addressing table with linear probing and splitmix-style hash;
 *       pass 2 — global count statistics (u64 exact sums);
 *       pass 3 — z-score flagging: z = (c - mean) / std, flag when
 *       ncells >= SENTINEL_MIN_CELLS && std > 0 && z >= threshold.
 *       Anomalies are then sorted by (bucket, service, ip).
 *
 * All floating-point comparisons happen in double on the host, so CPU and
 * GPU produce bit-identical decisions.
 */
#include "tokenizer.h"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <cstring>

namespace {

constexpr uint64_t TABLE_SLOTS = 1u << 21; /* power of two */
constexpr uint32_t SENTINEL_MIN_CELLS = 32;

static inline uint64_t cell_hash(uint64_t key) {
    return key * 0x9E3779B97F4A7C15ull;
}

static inline uint32_t slot_of(uint64_t key) {
    return (uint32_t)((cell_hash(key) >> 32) & (TABLE_SLOTS - 1));
}

struct Table {
    uint64_t* keys;   /* biased key, 0 = empty */
    uint32_t* counts;

    void add(uint64_t key, uint32_t count) {
        uint64_t skey = key | SENTINEL_KEY_BIAS;
        uint32_t s = slot_of(skey);
        for (;;) {
            uint64_t k = keys[s];
            if (k == skey) {
                counts[s] += count;
                return;
            }
            if (k == 0) { /* single-threaded path: direct store */
                keys[s] = skey;
                counts[s] = count;
                return;
            }
            s = (s + 1) & (TABLE_SLOTS - 1);
        }
    }
};

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

} // namespace

extern "C" int sentinel_detect_cpu(const sentinel_input* in,
                                   const sentinel_config* cfg,
                                   sentinel_results* out) {
    if (!in || !cfg || !out || !in->data || !in->line_offsets || !in->line_ip ||
        !in->line_bucket || !in->line_service) {
        return -1;
    }
    const uint32_t n = in->line_count;

    memset(out, 0, sizeof(*out));
    out->line_flags = (uint32_t*)calloc(n ? n : 1, sizeof(uint32_t));
    if (!out->line_flags) return -2;

    /* ---------------- K1: parallel pattern matcher (reference = serial) */
    for (uint32_t li = 0; li < n; ++li) {
        const uint8_t* lp = in->data + in->line_offsets[li];
        size_t llen = in->line_offsets[li + 1] - in->line_offsets[li];
        uint32_t flags = 0;
        for (uint32_t p = 0; p < cfg->pattern_count && p < SENTINEL_MAX_PATTERNS; ++p) {
            const char* pat = cfg->patterns[p];
            size_t plen = strlen(pat);
            if (plen == 0 || plen > SENTINEL_MAX_PATTERN_LEN || llen < plen) continue;
            for (size_t i = 0; i + plen <= llen; ++i) {
                if (memcmp(lp + i, pat, plen) == 0) {
                    flags |= (1u << p);
                    break;
                }
            }
        }
        out->line_flags[li] = flags;
    }

    /* ---------------- K2 pass 1: per-cell line counts */
    Table t;
    t.keys = (uint64_t*)calloc(TABLE_SLOTS, sizeof(uint64_t));
    t.counts = (uint32_t*)calloc(TABLE_SLOTS, sizeof(uint32_t));
    if (!t.keys || !t.counts) {
        free(t.keys); free(t.counts); free(out->line_flags);
        memset(out, 0, sizeof(*out));
        return -2;
    }

    for (uint32_t li = 0; li < n; ++li) {
        uint64_t key = sentinel_cell_key(in->line_ip[li], in->line_bucket[li],
                                         in->line_service[li]);
        t.add(key, 1);
    }

    /* ---------------- K2 pass 2: global statistics (exact integer sums) */
    unsigned long long sum = 0, sumsq = 0, ncells = 0;
    for (uint64_t s = 0; s < TABLE_SLOTS; ++s) {
        uint32_t c = t.counts[s];
        if (c) {
            sum += c;
            sumsq += (unsigned long long)c * c;
            ++ncells;
        }
    }
    /* Expose the baseline statistics even when there are too few cells to
     * flag anomalies (the Python binding verifies these values). */
    out->cells = ncells;
    out->mean = ncells ? (double)sum / (double)ncells : 0.0;
    {
        double var = ncells
            ? (double)sumsq / (double)ncells - out->mean * out->mean : 0.0;
        out->std = var > 0.0 ? sqrt(var) : 0.0;
    }

    /* ---------------- K2 pass 3: z-score flagging (host double math) */
    out->anomaly_records = nullptr;
    out->anomaly_count = 0;
    if (ncells >= SENTINEL_MIN_CELLS) {
        const double mean = out->mean;
        const double std = out->std;

        AnomRec* hits = (AnomRec*)malloc(sizeof(AnomRec) * (ncells + 1));
        if (!hits) {
            free(t.keys); free(t.counts); free(out->line_flags);
            memset(out, 0, sizeof(*out));
            return -2;
        }
        uint32_t nhits = 0;
        for (uint64_t s = 0; s < TABLE_SLOTS; ++s) {
            uint32_t c = t.counts[s];
            if (!c) continue;
            double z = (std > 0.0) ? ((double)c - mean) / std : 0.0;
            if (z >= (double)cfg->z_threshold) {
                uint64_t key = sentinel_key_decode(t.keys[s]);
                hits[nhits++] = {sentinel_key_bucket(key),
                                 sentinel_key_service(key),
                                 sentinel_key_ip(key), c};
            }
        }
        std::sort(hits, hits + nhits, anom_less);
        out->anomaly_count = nhits;
        if (nhits) {
            out->anomaly_records = (uint32_t*)malloc(sizeof(uint32_t) * 4 * nhits);
            if (!out->anomaly_records) {
                free(hits); free(t.keys); free(t.counts); free(out->line_flags);
                memset(out, 0, sizeof(*out));
                return -2;
            }
            for (uint32_t i = 0; i < nhits; ++i) {
                out->anomaly_records[4 * i + 0] = hits[i].ip;
                out->anomaly_records[4 * i + 1] = hits[i].bucket;
                out->anomaly_records[4 * i + 2] = hits[i].service;
                out->anomaly_records[4 * i + 3] = hits[i].count;
            }
        }
        free(hits);
    }

    free(t.keys);
    free(t.counts);
    return 0;
}
