/*
 * sentinel.h — public C API for the Sentinel-GPU detector library.
 *
 * One library is built per backend from the same sources:
 *   libsentinel_cpu.so — sequential reference (no CUDA required)
 *   libsentinel.so     — CUDA implementation
 * Both export the same `sentinel_detect` symbol, so a worker can swap
 * artifacts without recompiling (the backend is pinned per artifact version).
 *
 * Contract: detection is a pure function of (input, config). The tokenizer is
 * shared, deterministic preprocessing; the detector consumes its output.
 */
#ifndef SENTINEL_SENTINEL_H
#define SENTINEL_SENTINEL_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define SENTINEL_MAX_PATTERNS 32
#define SENTINEL_MAX_PATTERN_LEN 63

/* Bumped whenever a struct layout, function signature or semantic contract
 * changes. Workers must check this before calling any other entry point. */
#define SENTINEL_ABI_VERSION 2u

/* Stable error codes returned by every entry point. */
enum {
    SENTINEL_OK = 0,
    SENTINEL_ERR_INVALID = -1,
    SENTINEL_ERR_OOM = -2,
    SENTINEL_ERR_CUDA = -3,
    SENTINEL_ERR_UNSUPPORTED = -4
};

/* Phase timings (milliseconds) and peak device allocation for one call.
 * CPU-only builds leave every field at zero. */
typedef struct {
    uint64_t peak_device_bytes;
    double alloc_ms;
    double h2d_ms;
    double kernel_ms;
    double d2h_ms;
    double total_ms;
} sentinel_timing;

typedef struct {
    const char* patterns[SENTINEL_MAX_PATTERNS];
    uint32_t pattern_count;
    /* K2: flag a (ip, bucket, service) cell when its z-score meets this. */
    float z_threshold;
} sentinel_config;

typedef struct {
    const uint8_t* data;          /* raw log bytes                        */
    size_t data_len;
    uint32_t line_count;          /* N                                    */
    const uint32_t* line_offsets; /* N+1; line i = [off[i], off[i+1])     */
    const uint32_t* line_ip;      /* N; IPv4 host order, 0 = none         */
    const uint16_t* line_bucket;  /* N; minute-of-day bucket              */
    const uint8_t* line_service;  /* N; service id (see tokenizer.h)      */
} sentinel_input;

typedef struct {
    /* N bitmasks; bit i set => pattern i matched that line (K1). */
    uint32_t* line_flags;
    /* K2 anomalies, sorted by (bucket, service, ip) for CPU/GPU parity. */
    uint32_t anomaly_count;
    uint32_t* anomaly_records; /* anomaly_count * 4: ip, bucket, service, count */
    /* K2 baseline statistics over all non-empty cells (populated by both
     * backends so Python can verify the statistics stage directly). */
    uint64_t cells;  /* number of non-empty (ip, bucket, service) cells */
    double mean;     /* exact integer mean of per-cell counts, 0 if no cells */
    double std;      /* population stddev; zero variance yields 0.0 */
    sentinel_timing timing; /* measured pipeline phases; zero on CPU path */
} sentinel_results;

/* ---- K2 sliding-window feature/score stage (S5-C) ----------------------
 * This is the GPU port of the Sprint 4 windowed scorer, NOT the global
 * histogram K2 above. The caller supplies validated feature rows (one per
 * matched event); the library aggregates them by the full identity tuple
 * (service, ip, status, window, rule) and scores each series against its
 * previous windows. See docs/algorithm-semantics.md §§5–6.
 */
#define SENTINEL_MAX_FEATURES (1u << 20) /* hash table stays <= 50% occupied */

typedef struct {
    uint32_t count;               /* number of rows; <= SENTINEL_MAX_FEATURES */
    const uint32_t* service;      /* count entries */
    const uint32_t* ip;           /* count entries, host-order IPv4 */
    const uint32_t* status;       /* count entries */
    const int64_t* window_id;     /* count entries */
    const uint32_t* rule_index;   /* count entries, index into rule config */
    const uint32_t* event_count;  /* count entries, >= 1, <= UINT32_MAX */
} sentinel_feature_input;

typedef struct {
    uint32_t baseline_windows;    /* 1..1440; same as DetectorSettings */
    float z_threshold;            /* (0, 1000] */
    uint32_t max_rule_index;      /* highest accepted rule_index; 0..255 */
} sentinel_window_config;

typedef struct {
    uint32_t feature_count;
    uint32_t* service;            /* aggregated identity + count */
    uint32_t* ip;
    uint32_t* status;
    int64_t* window_id;
    uint32_t* rule_index;
    uint32_t* count;

    uint32_t alert_count;
    uint32_t* alert_service;
    uint32_t* alert_ip;
    uint32_t* alert_status;
    int64_t* alert_window;
    uint32_t* alert_rule;
    uint32_t* alert_count_out; /* alert_count entries; name avoids count clash */
    double* alert_mean;
    double* alert_std;
    double* alert_score;

    sentinel_timing timing;
} sentinel_window_results;

int sentinel_detect_window_gpu(const sentinel_feature_input* in,
                               const sentinel_window_config* cfg,
                               sentinel_window_results* out);
void sentinel_free_window_results(sentinel_window_results* out);

/* Shared deterministic tokenizer. Outputs are malloc'd; caller free()s. */
int sentinel_tokenize(const uint8_t* data, size_t len,
                      uint32_t** line_offsets, uint32_t** line_ip,
                      uint16_t** line_bucket, uint8_t** line_service,
                      uint32_t* line_count_out);

/* Backend entry points. `sentinel_detect` is the swappable symbol. */
int sentinel_detect_cpu(const sentinel_input* in, const sentinel_config* cfg,
                        sentinel_results* out);
int sentinel_detect_gpu(const sentinel_input* in, const sentinel_config* cfg,
                        sentinel_results* out);

/* Free results allocated by either backend. Safe on zeroed results. */
void sentinel_free_results(sentinel_results* out);

/* Version/build identity. Call once before using the rest of the ABI. */
uint32_t sentinel_abi_version(void);
const char* sentinel_build_id(void);

#ifdef __cplusplus
}
#endif

#endif /* SENTINEL_SENTINEL_H */
