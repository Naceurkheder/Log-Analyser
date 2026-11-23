/*
 * window_kernels.cu — CUDA port of the Sprint 4 windowed K2 scorer.
 *
 * This is deliberately distinct from detect_gpu.cu's global-histogram K2:
 * the caller supplies already-extracted feature rows and this stage reproduces
 * docs/algorithm-semantics.md §§5–6 exactly:
 *   1. aggregate rows by the FULL tuple (service, ip, status, window, rule);
 *   2. for each series, build its baseline from the previous N present/empty
 *      windows starting no earlier than the series' first window;
 *   3. emit scored alerts only when at least two history points exist and the
 *      population standard deviation is positive.
 *
 * Hash collisions are resolved by comparing the entire identity tuple, so no
 * key is ever dropped. Counts are uint32_t; overflow is detected atomically
 * and returned as SENTINEL_ERR_INVALID instead of wrapping.
 */
#include "sentinel/sentinel.h"

#include <cuda_runtime.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <unordered_map>
#include <vector>

namespace {

constexpr uint32_t TABLE_SLOTS = 1u << 21;
constexpr uint32_t TABLE_MASK = TABLE_SLOTS - 1;
constexpr uint32_t U32_MAX = 0xFFFFFFFFu;

#define CUDA_CHECK(call)                                                       \
    do {                                                                       \
        cudaError_t err_ = (call);                                             \
        if (err_ != cudaSuccess) {                                             \
            bool oom_ = err_ == cudaErrorMemoryAllocation;                     \
            fprintf(stderr, "CUDA %s %s at %s:%d: %s\n",                       \
                    oom_ ? "OOM" : "error", #call, __FILE__, __LINE__,         \
                    cudaGetErrorString(err_));                                 \
            return oom_ ? SENTINEL_ERR_OOM : SENTINEL_ERR_CUDA;                \
        }                                                                      \
    } while (0)

#define CUDA_LAUNCH_CHECK(...)                                                 \
    do {                                                                       \
        (__VA_ARGS__);                                                         \
        CUDA_CHECK(cudaGetLastError());                                        \
    } while (0)

static double now_ms() {
    return std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

struct FeatureKey {
    uint32_t service;
    uint32_t ip;
    uint32_t status;
    int64_t window;
    uint32_t rule;
};

struct WindowTable {
    uint64_t* keys;    /* hash value, 0 = empty */
    uint32_t* claimed; /* 0 = free, 1 = being written or written */
    uint32_t* index;   /* aggregated row index + 1; written before keys */
};

struct AggArrays {
    uint32_t* service;
    uint32_t* ip;
    uint32_t* status;
    int64_t* window;
    uint32_t* rule;
    uint32_t* count;
};

struct AlertRow {
    uint32_t service;
    uint32_t ip;
    uint32_t status;
    int64_t window;
    uint32_t rule;
    uint32_t count;
    double mean;
    double std;
    double score;
};

__device__ __forceinline__ uint64_t feature_hash(const FeatureKey& k) {
    uint64_t h = 1469598103934665603ull; /* FNV-1a offset basis */
    const uint64_t words[5] = {
        k.service, k.ip, k.status, (uint64_t)k.window, k.rule
    };
#pragma unroll
    for (int i = 0; i < 5; ++i) {
        h ^= words[i];
        h *= 1099511628211ull;
    }
    return h == 0 ? 1ull : h;
}

__device__ __forceinline__ bool same_key(const FeatureKey& a,
                                         const FeatureKey& b) {
    return a.service == b.service && a.ip == b.ip && a.status == b.status &&
           a.window == b.window && a.rule == b.rule;
}

__device__ __forceinline__ uint32_t find_slot(WindowTable t, AggArrays agg,
                                               const FeatureKey& key) {
    const uint64_t hash = feature_hash(key);
    uint32_t slot = (uint32_t)hash & TABLE_MASK;
    for (;;) {
        uint64_t current = t.keys[slot];
        if (current == 0) return U32_MAX;
        if (current == hash) {
            uint32_t row = t.index[slot] - 1;
            FeatureKey stored{agg.service[row], agg.ip[row], agg.status[row],
                              agg.window[row], agg.rule[row]};
            if (same_key(stored, key)) return row;
        }
        slot = (slot + 1) & TABLE_MASK;
    }
}

/* pass 1: claim/open-address each full identity, then sum its counts. */
__global__ void aggregate_kernel(
    const uint32_t* service, const uint32_t* ip, const uint32_t* status,
    const int64_t* window, const uint32_t* rule, const uint32_t* events,
    uint32_t n, WindowTable table, AggArrays agg, uint32_t* next,
    uint32_t* overflow) {
    for (uint32_t i = blockIdx.x * blockDim.x + threadIdx.x; i < n;
         i += gridDim.x * blockDim.x) {
        FeatureKey key{service[i], ip[i], status[i], window[i], rule[i]};
        const uint64_t hash = feature_hash(key);
        uint32_t slot = (uint32_t)hash & TABLE_MASK;
        for (;;) {
            if (atomicCAS(&table.claimed[slot], 0u, 1u) == 0u) {
                uint32_t row = atomicAdd(next, 1u);
                agg.service[row] = key.service;
                agg.ip[row] = key.ip;
                agg.status[row] = key.status;
                agg.window[row] = key.window;
                agg.rule[row] = key.rule;
                agg.count[row] = events[i];
                table.index[slot] = row + 1;
                __threadfence(); /* identity/index visible before hash publish */
                table.keys[slot] = hash;
                break;
            }
            /* Wait for the claiming thread to publish its hash. */
            volatile unsigned long long* published =
                reinterpret_cast<volatile unsigned long long*>(table.keys);
            while (published[slot] == 0ull) {}
            if (published[slot] == hash) {
                uint32_t row = table.index[slot] - 1;
                FeatureKey stored{agg.service[row], agg.ip[row], agg.status[row],
                                  agg.window[row], agg.rule[row]};
                if (same_key(stored, key)) {
                    uint32_t value = events[i];
                    uint32_t old = atomicAdd(&agg.count[row], value);
                    if (old > U32_MAX - value) atomicExch(overflow, 1u);
                    break;
                }
            }
            slot = (slot + 1) & TABLE_MASK;
        }
    }
}

/* pass 2: baseline + z-score against the same window table. */
__global__ void score_kernel(
    WindowTable table, AggArrays agg, const int64_t* row_min, uint32_t n,
    uint32_t baseline, float z_threshold, uint32_t* alert_next,
    AlertRow* alerts) {
    for (uint32_t i = blockIdx.x * blockDim.x + threadIdx.x; i < n;
         i += gridDim.x * blockDim.x) {
        const int64_t target = agg.window[i]; /* never mutated inside the loop */
        FeatureKey series{agg.service[i], agg.ip[i], agg.status[i],
                          target, agg.rule[i]};
        double sum = 0.0, sum_sq = 0.0;
        uint32_t history = 0;
        for (uint32_t back = 1; back <= baseline; ++back) {
            int64_t prior = target - (int64_t)back;
            if (prior < row_min[i]) break; /* no earlier windows in this series */
            series.window = prior;
            uint32_t row = find_slot(table, agg, series);
            uint32_t count = row == U32_MAX ? 0u : agg.count[row];
            sum += (double)count;
            sum_sq += (double)count * (double)count;
            ++history;
        }
        if (history < 2) continue;
        double mean = sum / (double)history;
        double variance = sum_sq / (double)history - mean * mean;
        if (variance < 0.0) variance = 0.0;
        double std = sqrt(variance);
        if (!(std > 0.0)) continue;
        double score = ((double)agg.count[i] - mean) / std;
        if (score < (double)z_threshold) continue;

        uint32_t out = atomicAdd(alert_next, 1u);
        alerts[out] = AlertRow{
            agg.service[i], agg.ip[i], agg.status[i], agg.window[i],
            agg.rule[i], agg.count[i], mean, std, score
        };
    }
}

struct SeriesKey {
    uint32_t service, ip, status, rule;
    bool operator==(const SeriesKey& other) const {
        return service == other.service && ip == other.ip &&
               status == other.status && rule == other.rule;
    }
};

struct SeriesHash {
    size_t operator()(const SeriesKey& k) const {
        uint64_t h = 1469598103934665603ull;
        const uint32_t words[4] = {k.service, k.ip, k.status, k.rule};
        for (uint32_t word : words) {
            h ^= word;
            h *= 1099511628211ull;
        }
        return (size_t)h;
    }
};

bool alert_less(const AlertRow& a, const AlertRow& b) {
    if (a.window != b.window) return a.window < b.window;
    if (a.service != b.service) return a.service < b.service;
    if (a.ip != b.ip) return a.ip < b.ip;
    if (a.status != b.status) return a.status < b.status;
    return a.rule < b.rule;
}

} // namespace

extern "C" int sentinel_detect_window_gpu(const sentinel_feature_input* in,
                                          const sentinel_window_config* cfg,
                                          sentinel_window_results* out) {
    if (!in || !cfg || !out) return SENTINEL_ERR_INVALID;
    const uint32_t n = in->count;
    const double call_start = now_ms();
    double mark = call_start, h2d_ms = 0.0, kernel_ms = 0.0, d2h_ms = 0.0;
    memset(out, 0, sizeof(*out));

    if (cfg->baseline_windows < 1 || cfg->baseline_windows > 1440 ||
        !(cfg->z_threshold > 0.0f) || cfg->z_threshold > 1000.0f ||
        cfg->max_rule_index > 255 || n > SENTINEL_MAX_FEATURES) {
        return SENTINEL_ERR_UNSUPPORTED;
    }
    if (n == 0) {
        out->timing.total_ms = now_ms() - call_start;
        return SENTINEL_OK;
    }
    if (!in->service || !in->ip || !in->status || !in->window_id ||
        !in->rule_index || !in->event_count) {
        return SENTINEL_ERR_INVALID;
    }
    for (uint32_t i = 0; i < n; ++i) {
        if (in->event_count[i] == 0 || in->rule_index[i] > cfg->max_rule_index) {
            return SENTINEL_ERR_INVALID;
        }
    }

    std::vector<uint32_t> h_service(n), h_ip(n), h_status(n), h_rule(n), h_count(n);
    std::vector<int64_t> h_window(n), h_row_min(n);
    std::vector<AlertRow> h_alerts(n);

    WindowTable table{};
    AggArrays agg{};
    uint32_t* d_service = nullptr;
    uint32_t* d_ip = nullptr;
    uint32_t* d_status = nullptr;
    int64_t* d_window = nullptr;
    uint32_t* d_rule = nullptr;
    uint32_t* d_events = nullptr;
    uint32_t* d_next = nullptr;
    uint32_t* d_overflow = nullptr;
    int64_t* d_row_min = nullptr;
    uint32_t* d_alert_next = nullptr;
    AlertRow* d_alerts = nullptr;
    int rc = SENTINEL_OK;

    do {
        CUDA_CHECK(cudaMalloc(&d_service, sizeof(uint32_t) * n));
        CUDA_CHECK(cudaMalloc(&d_ip, sizeof(uint32_t) * n));
        CUDA_CHECK(cudaMalloc(&d_status, sizeof(uint32_t) * n));
        CUDA_CHECK(cudaMalloc(&d_window, sizeof(int64_t) * n));
        CUDA_CHECK(cudaMalloc(&d_rule, sizeof(uint32_t) * n));
        CUDA_CHECK(cudaMalloc(&d_events, sizeof(uint32_t) * n));
        CUDA_CHECK(cudaMalloc(&table.keys, sizeof(uint64_t) * TABLE_SLOTS));
        CUDA_CHECK(cudaMalloc(&table.claimed, sizeof(uint32_t) * TABLE_SLOTS));
        CUDA_CHECK(cudaMalloc(&table.index, sizeof(uint32_t) * TABLE_SLOTS));
        CUDA_CHECK(cudaMalloc(&agg.service, sizeof(uint32_t) * n));
        CUDA_CHECK(cudaMalloc(&agg.ip, sizeof(uint32_t) * n));
        CUDA_CHECK(cudaMalloc(&agg.status, sizeof(uint32_t) * n));
        CUDA_CHECK(cudaMalloc(&agg.window, sizeof(int64_t) * n));
        CUDA_CHECK(cudaMalloc(&agg.rule, sizeof(uint32_t) * n));
        CUDA_CHECK(cudaMalloc(&agg.count, sizeof(uint32_t) * n));
        CUDA_CHECK(cudaMalloc(&d_next, sizeof(uint32_t)));
        CUDA_CHECK(cudaMalloc(&d_overflow, sizeof(uint32_t)));
        CUDA_CHECK(cudaMalloc(&d_row_min, sizeof(int64_t) * n));
        CUDA_CHECK(cudaMalloc(&d_alerts, sizeof(AlertRow) * n));
        CUDA_CHECK(cudaMalloc(&d_alert_next, sizeof(uint32_t)));
        out->timing.alloc_ms = now_ms() - mark;
        mark = now_ms();

        CUDA_CHECK(cudaMemset(table.keys, 0, sizeof(uint64_t) * TABLE_SLOTS));
        CUDA_CHECK(cudaMemset(table.claimed, 0, sizeof(uint32_t) * TABLE_SLOTS));
        CUDA_CHECK(cudaMemset(table.index, 0, sizeof(uint32_t) * TABLE_SLOTS));
        CUDA_CHECK(cudaMemset(d_next, 0, sizeof(uint32_t)));
        CUDA_CHECK(cudaMemset(d_overflow, 0, sizeof(uint32_t)));
        CUDA_CHECK(cudaMemset(d_alert_next, 0, sizeof(uint32_t)));

        mark = now_ms();
        CUDA_CHECK(cudaMemcpy(d_service, in->service, sizeof(uint32_t) * n,
                              cudaMemcpyHostToDevice));
        CUDA_CHECK(cudaMemcpy(d_ip, in->ip, sizeof(uint32_t) * n,
                              cudaMemcpyHostToDevice));
        CUDA_CHECK(cudaMemcpy(d_status, in->status, sizeof(uint32_t) * n,
                              cudaMemcpyHostToDevice));
        CUDA_CHECK(cudaMemcpy(d_window, in->window_id, sizeof(int64_t) * n,
                              cudaMemcpyHostToDevice));
        CUDA_CHECK(cudaMemcpy(d_rule, in->rule_index, sizeof(uint32_t) * n,
                              cudaMemcpyHostToDevice));
        CUDA_CHECK(cudaMemcpy(d_events, in->event_count, sizeof(uint32_t) * n,
                              cudaMemcpyHostToDevice));
        h2d_ms += now_ms() - mark;

        const int block = 256;
        const int grid = (int)std::min<uint64_t>(2048, (n + block - 1) / block);
        mark = now_ms();
        CUDA_LAUNCH_CHECK(aggregate_kernel<<<grid, block>>>(
            d_service, d_ip, d_status, d_window, d_rule, d_events, n,
            table, agg, d_next, d_overflow));
        kernel_ms += now_ms() - mark;

        uint32_t aggregated = 0, overflow = 0;
        mark = now_ms();
        CUDA_CHECK(cudaMemcpy(&aggregated, d_next, sizeof(uint32_t),
                              cudaMemcpyDeviceToHost));
        CUDA_CHECK(cudaMemcpy(&overflow, d_overflow, sizeof(uint32_t),
                              cudaMemcpyDeviceToHost));
        d2h_ms += now_ms() - mark;
        if (overflow) { rc = SENTINEL_ERR_INVALID; break; }

        mark = now_ms();
        CUDA_CHECK(cudaMemcpy(h_service.data(), agg.service,
                              sizeof(uint32_t) * aggregated,
                              cudaMemcpyDeviceToHost));
        CUDA_CHECK(cudaMemcpy(h_ip.data(), agg.ip, sizeof(uint32_t) * aggregated,
                              cudaMemcpyDeviceToHost));
        CUDA_CHECK(cudaMemcpy(h_status.data(), agg.status,
                              sizeof(uint32_t) * aggregated,
                              cudaMemcpyDeviceToHost));
        CUDA_CHECK(cudaMemcpy(h_window.data(), agg.window,
                              sizeof(int64_t) * aggregated,
                              cudaMemcpyDeviceToHost));
        CUDA_CHECK(cudaMemcpy(h_rule.data(), agg.rule, sizeof(uint32_t) * aggregated,
                              cudaMemcpyDeviceToHost));
        CUDA_CHECK(cudaMemcpy(h_count.data(), agg.count, sizeof(uint32_t) * aggregated,
                              cudaMemcpyDeviceToHost));
        d2h_ms += now_ms() - mark;

        /* Series starts are a host-side O(N) reduction; only the compact
         * per-row minimum array is sent back to the scoring kernel. */
        std::unordered_map<SeriesKey, int64_t, SeriesHash> starts;
        for (uint32_t i = 0; i < aggregated; ++i) {
            SeriesKey key{h_service[i], h_ip[i], h_status[i], h_rule[i]};
            auto it = starts.find(key);
            if (it == starts.end()) starts.emplace(key, h_window[i]);
            else if (h_window[i] < it->second) it->second = h_window[i];
        }
        for (uint32_t i = 0; i < aggregated; ++i) {
            SeriesKey key{h_service[i], h_ip[i], h_status[i], h_rule[i]};
            h_row_min[i] = starts.find(key)->second;
        }
        mark = now_ms();
        CUDA_CHECK(cudaMemcpy(d_row_min, h_row_min.data(),
                              sizeof(int64_t) * aggregated,
                              cudaMemcpyHostToDevice));
        h2d_ms += now_ms() - mark;

        mark = now_ms();
        CUDA_LAUNCH_CHECK(score_kernel<<<grid, block>>>(
            table, agg, d_row_min, aggregated, cfg->baseline_windows,
            cfg->z_threshold, d_alert_next, d_alerts));
        kernel_ms += now_ms() - mark;

        uint32_t alert_count = 0;
        mark = now_ms();
        CUDA_CHECK(cudaMemcpy(&alert_count, d_alert_next, sizeof(uint32_t),
                              cudaMemcpyDeviceToHost));
        if (alert_count) {
            CUDA_CHECK(cudaMemcpy(h_alerts.data(), d_alerts,
                                  sizeof(AlertRow) * alert_count,
                                  cudaMemcpyDeviceToHost));
        }
        d2h_ms += now_ms() - mark;
        std::sort(h_alerts.begin(), h_alerts.begin() + alert_count, alert_less);

        out->feature_count = aggregated;
        out->service = (uint32_t*)malloc(sizeof(uint32_t) * aggregated);
        out->ip = (uint32_t*)malloc(sizeof(uint32_t) * aggregated);
        out->status = (uint32_t*)malloc(sizeof(uint32_t) * aggregated);
        out->window_id = (int64_t*)malloc(sizeof(int64_t) * aggregated);
        out->rule_index = (uint32_t*)malloc(sizeof(uint32_t) * aggregated);
        out->count = (uint32_t*)malloc(sizeof(uint32_t) * aggregated);
        out->alert_count = alert_count;
        out->alert_service = (uint32_t*)malloc(sizeof(uint32_t) * alert_count);
        out->alert_ip = (uint32_t*)malloc(sizeof(uint32_t) * alert_count);
        out->alert_status = (uint32_t*)malloc(sizeof(uint32_t) * alert_count);
        out->alert_window = (int64_t*)malloc(sizeof(int64_t) * alert_count);
        out->alert_rule = (uint32_t*)malloc(sizeof(uint32_t) * alert_count);
        out->alert_count_out = (uint32_t*)malloc(sizeof(uint32_t) * alert_count);
        out->alert_mean = (double*)malloc(sizeof(double) * alert_count);
        out->alert_std = (double*)malloc(sizeof(double) * alert_count);
        out->alert_score = (double*)malloc(sizeof(double) * alert_count);
        if (!out->service || !out->ip || !out->status || !out->window_id ||
            !out->rule_index || !out->count ||
            (alert_count && (!out->alert_service || !out->alert_ip ||
                             !out->alert_status || !out->alert_window ||
                             !out->alert_rule || !out->alert_count_out ||
                             !out->alert_mean || !out->alert_std ||
                             !out->alert_score))) {
            rc = SENTINEL_ERR_OOM;
            break;
        }
        for (uint32_t i = 0; i < aggregated; ++i) {
            out->service[i] = h_service[i];
            out->ip[i] = h_ip[i];
            out->status[i] = h_status[i];
            out->window_id[i] = h_window[i];
            out->rule_index[i] = h_rule[i];
            out->count[i] = h_count[i];
        }
        for (uint32_t i = 0; i < alert_count; ++i) {
            const AlertRow& a = h_alerts[i];
            out->alert_service[i] = a.service;
            out->alert_ip[i] = a.ip;
            out->alert_status[i] = a.status;
            out->alert_window[i] = a.window;
            out->alert_rule[i] = a.rule;
            out->alert_count_out[i] = a.count;
            out->alert_mean[i] = a.mean;
            out->alert_std[i] = a.std;
            out->alert_score[i] = a.score;
        }
    } while (0);

    out->timing.h2d_ms = h2d_ms;
    out->timing.kernel_ms = kernel_ms;
    out->timing.d2h_ms = d2h_ms;
    out->timing.total_ms = now_ms() - call_start;
    out->timing.peak_device_bytes =
        (uint64_t)sizeof(uint32_t) * n * 4
        + (uint64_t)sizeof(int64_t) * n * 2
        + (uint64_t)(sizeof(uint64_t) + 2 * sizeof(uint32_t)) * TABLE_SLOTS
        + (uint64_t)sizeof(AlertRow) * n
        + (uint64_t)sizeof(uint32_t) * 4;

    if (d_service) cudaFree(d_service);
    if (d_ip) cudaFree(d_ip);
    if (d_status) cudaFree(d_status);
    if (d_window) cudaFree(d_window);
    if (d_rule) cudaFree(d_rule);
    if (d_events) cudaFree(d_events);
    if (table.keys) cudaFree(table.keys);
    if (table.claimed) cudaFree(table.claimed);
    if (table.index) cudaFree(table.index);
    if (agg.service) cudaFree(agg.service);
    if (agg.ip) cudaFree(agg.ip);
    if (agg.status) cudaFree(agg.status);
    if (agg.window) cudaFree(agg.window);
    if (agg.rule) cudaFree(agg.rule);
    if (agg.count) cudaFree(agg.count);
    if (d_next) cudaFree(d_next);
    if (d_overflow) cudaFree(d_overflow);
    if (d_row_min) cudaFree(d_row_min);
    if (d_alerts) cudaFree(d_alerts);
    if (d_alert_next) cudaFree(d_alert_next);

    if (rc != SENTINEL_OK) {
        sentinel_free_window_results(out);
        return rc;
    }
    return SENTINEL_OK;
}

extern "C" void sentinel_free_window_results(sentinel_window_results* out) {
    if (!out) return;
    free(out->service);
    free(out->ip);
    free(out->status);
    free(out->window_id);
    free(out->rule_index);
    free(out->count);
    free(out->alert_service);
    free(out->alert_ip);
    free(out->alert_status);
    free(out->alert_window);
    free(out->alert_rule);
    free(out->alert_count_out);
    free(out->alert_mean);
    free(out->alert_std);
    free(out->alert_score);
    memset(out, 0, sizeof(*out));
}
