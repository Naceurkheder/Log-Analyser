/*
 * tokenizer.h — internal contract shared by both detector backends.
 * Keep the K2 key encoding and service ids in lockstep with detect_gpu.cu.
 */
#ifndef SENTINEL_TOKENIZER_H
#define SENTINEL_TOKENIZER_H

#include "sentinel/sentinel.h"

#ifdef __CUDACC__
#define SENTINEL_HD __host__ __device__
#else
#define SENTINEL_HD
#endif

/* Service ids (order matters; first match wins in the tokenizer). */
enum {
    SENTINEL_SERVICE_UNKNOWN = 0,
    SENTINEL_SERVICE_SSHD = 1,
    SENTINEL_SERVICE_NGINX = 2,
    SENTINEL_SERVICE_APP = 3,
    SENTINEL_SERVICE_KERNEL = 4,
    SENTINEL_SERVICE_AUTH = 5,
};

/* K2 histogram cell key: (ip, bucket, service) packed into 48 bits.
 * key = (ip << 16) | (bucket << 5) | service   [bucket < 1440 (11 bits)]
 * Stored keys are biased with bit 62 (above the 48-bit key space) so that
 * 0 remains the empty-slot marker; decoding strips the bias bit. */
static constexpr uint64_t SENTINEL_KEY_BIAS = 1ull << 62;

SENTINEL_HD static inline uint64_t sentinel_cell_key(uint32_t ip, uint16_t bucket,
                                         uint8_t service) {
    return (((uint64_t)ip << 16) | ((uint64_t)(bucket & 0x7FF) << 5) |
            (uint64_t)(service & 0x1F)) | SENTINEL_KEY_BIAS;
}

SENTINEL_HD static inline uint64_t sentinel_key_decode(uint64_t stored) {
    return stored & ~SENTINEL_KEY_BIAS;
}
SENTINEL_HD static inline uint32_t sentinel_key_ip(uint64_t key) {
    return (uint32_t)(key >> 16);
}
SENTINEL_HD static inline uint16_t sentinel_key_bucket(uint64_t key) {
    return (uint16_t)((key >> 5) & 0x7FF);
}
SENTINEL_HD static inline uint8_t sentinel_key_service(uint64_t key) {
    return (uint8_t)(key & 0x1F);
}

#endif /* SENTINEL_TOKENIZER_H */
