/*
 * tokenizer.cpp — deterministic shared preprocessing.
 *
 * Extracts, per line: byte offsets, the first embedded IPv4 address, the
 * minute-of-day time bucket and a service id. Deliberately single-pass and
 * allocation-light; it is shared by CPU and GPU paths so benchmark deltas
 * measure detection, not parsing.
 */
#include "tokenizer.h"

#include <cstdlib>
#include <cstring>

namespace {

static inline int digit(uint8_t c) { return c >= '0' && c <= '9'; }

/* Try to parse an IPv4 starting exactly at data[i]; returns length 0 on failure. */
static size_t try_parse_ipv4(const uint8_t* d, size_t i, size_t len,
                             uint32_t* out) {
    uint32_t ip = 0;
    size_t p = i;

    /* leading boundary: must not be glued to a preceding digit */
    if (i > 0 && digit(d[i - 1])) return 0;

    for (int octet = 0; octet < 4; ++octet) {
        if (octet > 0) {
            if (p >= len || d[p] != '.') return 0;
            ++p;
        }
        int v = 0, nd = 0;
        while (p < len && digit(d[p]) && nd < 3) {
            v = v * 10 + (d[p] - '0');
            ++p; ++nd;
        }
        if (nd == 0 || v > 255) return 0;
        if (p < len && digit(d[p])) return 0; /* >3 digits glued */
        ip = (ip << 8) | (uint32_t)v;
    }
    /* trailing boundary: no digit or dot glued to the last octet */
    if (p < len && (digit(d[p]) || d[p] == '.')) return 0;

    *out = ip;
    return p - i;
}

static size_t find_first_ipv4(const uint8_t* d, size_t len, uint32_t* out) {
    for (size_t i = 0; i + 7 <= len; ++i) {
        if (!digit(d[i])) continue;
        size_t n = try_parse_ipv4(d, i, len, out);
        if (n) return n;
    }
    return 0;
}

/* Find the first hh:mm:ss occurrence; bucket = hh*60 + mm. */
static int find_time_bucket(const uint8_t* d, size_t len, uint16_t* out) {
    for (size_t i = 0; i + 8 <= len; ++i) {
        if (!digit(d[i]) || d[i + 2] != ':' || d[i + 5] != ':') continue;
        int hh = (d[i] - '0') * 10 + (d[i + 1] - '0');
        int mm = (d[i + 3] - '0') * 10 + (d[i + 4] - '0');
        int ss = (d[i + 6] - '0') * 10 + (d[i + 7] - '0');
        if (d[i + 1] > '5' + 4) continue; /* cheap sanity, real check below */
        if (hh > 23 || mm > 59 || ss > 59) continue;
        *out = (uint16_t)(hh * 60 + mm);
        return 1;
    }
    return 0;
}

static inline int starts_with(const uint8_t* d, size_t len, size_t i,
                              const char* s) {
    size_t n = strlen(s);
    if (i + n > len) return 0;
    return memcmp(d + i, s, n) == 0;
}

static uint8_t find_service(const uint8_t* d, size_t len) {
    static const struct { const char* tag; uint8_t id; } tags[] = {
        {"sshd[", SENTINEL_SERVICE_SSHD},
        {"nginx[", SENTINEL_SERVICE_NGINX},
        {"app-server[", SENTINEL_SERVICE_APP},
        {"kernel[", SENTINEL_SERVICE_KERNEL},
        {"auth-daemon[", SENTINEL_SERVICE_AUTH},
    };
    for (size_t i = 0; i + 6 <= len; ++i) {
        for (auto& t : tags) {
            if (starts_with(d, len, i, t.tag)) return t.id;
        }
    }
    return SENTINEL_SERVICE_UNKNOWN;
}

} // namespace

extern "C" int sentinel_tokenize(const uint8_t* data, size_t len,
                                 uint32_t** line_offsets, uint32_t** line_ip,
                                 uint16_t** line_bucket,
                                 uint8_t** line_service,
                                 uint32_t* line_count_out) {
    if (!data || !line_offsets || !line_ip || !line_bucket || !line_service ||
        !line_count_out) {
        return -1;
    }

    /* count lines */
    uint32_t n = 0;
    for (size_t i = 0; i < len; ++i) n += (data[i] == '\n');

    /* n LF-delimited records, plus an optional unterminated final record;
     * N+1 offsets are needed for N records. */
    uint32_t* offs = (uint32_t*)malloc(sizeof(uint32_t) * ((size_t)n + 2));
    uint32_t* ips = (uint32_t*)malloc(sizeof(uint32_t) * (n ? n : 1));
    uint16_t* buckets = (uint16_t*)malloc(sizeof(uint16_t) * (n ? n : 1));
    uint8_t* services = (uint8_t*)malloc(sizeof(uint8_t) * (n ? n : 1));
    if (!offs || !ips || !buckets || !services) {
        free(offs); free(ips); free(buckets); free(services);
        return -2;
    }

    uint32_t li = 0;
    size_t start = 0;
    auto emit_record = [&](size_t end) {
        size_t llen = (end > start) ? end - start : 0;
        const uint8_t* lp = data + start;

        uint32_t ip = 0;
        find_first_ipv4(lp, llen, &ip);

        uint16_t bucket = 0;
        find_time_bucket(lp, llen, &bucket);

        offs[li] = (uint32_t)start;
        ips[li] = ip;
        buckets[li] = bucket;
        services[li] = llen ? find_service(lp, llen)
                            : (uint8_t)SENTINEL_SERVICE_UNKNOWN;
        ++li;
        start = end + 1; /* skip the LF */
    };
    /* LF-delimited records. A trailing LF ends the last record; it does not
     * create an extra empty one. A final unterminated record is included. */
    for (size_t i = 0; i < len; ++i) {
        if (data[i] == '\n') emit_record(i);
    }
    if (start < len) emit_record(len);
    offs[li] = (uint32_t)len;

    *line_offsets = offs;
    *line_ip = ips;
    *line_bucket = buckets;
    *line_service = services;
    *line_count_out = li;
    return 0;
}

extern "C" void sentinel_free_results(sentinel_results* out) {
    if (!out) return;
    free(out->line_flags);
    free(out->anomaly_records);
    out->line_flags = nullptr;
    out->anomaly_records = nullptr;
    out->anomaly_count = 0;
    out->cells = 0;
    out->mean = 0.0;
    out->std = 0.0;
}
