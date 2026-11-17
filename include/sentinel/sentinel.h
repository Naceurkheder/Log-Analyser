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
} sentinel_results;

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

#ifdef __cplusplus
}
#endif

#endif /* SENTINEL_SENTINEL_H */
