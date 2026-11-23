/*
 * test_parity.cpp — Sprint 2 acceptance: CPU and GPU outputs agree on the
 * fixed test log file (flagged lines and anomalies must be byte-identical).
 *
 * Also asserts the fixture actually exercises every pattern class so a
 * silently-degenerate kernel can't pass vacuously.
 */
#include "sentinel/sentinel.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

static const char* PATTERNS[] = {
    "Failed password", "Invalid user", "ERROR", " 404 ", "SEGFAULT",
};
#define N_PATTERNS 5

static int failures = 0;
#define CHECK(cond, msg)                                                    \
    do {                                                                    \
        if (!(cond)) {                                                      \
            fprintf(stderr, "  FAIL %s\n", msg);                            \
            ++failures;                                                     \
        } else {                                                            \
            printf("  PASS %s\n", msg);                                     \
        }                                                                   \
    } while (0)

int main(int argc, char** argv) {
    const char* path = argc > 1 ? argv[1] : "tests/fixtures/sample.log";
    FILE* f = fopen(path, "rb");
    if (!f) {
        fprintf(stderr, "cannot open fixture %s\n", path);
        return 2;
    }
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    std::vector<uint8_t> data((size_t)sz);
    if (fread(data.data(), 1, (size_t)sz, f) != (size_t)sz) return 2;
    fclose(f);

    sentinel_input in{};
    uint32_t* offs = nullptr;
    uint32_t* ips = nullptr;
    uint16_t* buckets = nullptr;
    uint8_t* services = nullptr;
    if (sentinel_tokenize(data.data(), data.size(), &offs, &ips, &buckets,
                          &services, &in.line_count)) {
        fprintf(stderr, "tokenize failed\n");
        return 2;
    }
    in.data = data.data();
    in.data_len = data.size();
    in.line_offsets = offs;
    in.line_ip = ips;
    in.line_bucket = buckets;
    in.line_service = services;

    sentinel_config cfg{};
    for (auto p : PATTERNS) cfg.patterns[cfg.pattern_count++] = p;
    cfg.z_threshold = 4.0f;

    sentinel_results cpu{}, gpu{};
    printf("== parity: cpu vs gpu on %s (%u lines) ==\n", path, in.line_count);

    int rc;
    if ((rc = sentinel_detect_cpu(&in, &cfg, &cpu))) {
        fprintf(stderr, "cpu detect failed: %d\n", rc);
        return 2;
    }
    if ((rc = sentinel_detect_gpu(&in, &cfg, &gpu))) {
        fprintf(stderr, "gpu detect failed: %d\n", rc);
        return 2;
    }

    CHECK(memcmp(cpu.line_flags, gpu.line_flags,
                 sizeof(uint32_t) * in.line_count) == 0,
          "K1 line flags identical (cpu == gpu)");

    CHECK(cpu.cells == gpu.cells, "K2 cell count identical (cpu == gpu)");
    CHECK(cpu.mean == gpu.mean, "K2 mean identical (cpu == gpu)");
    CHECK(cpu.std == gpu.std, "K2 stddev identical (cpu == gpu)");
    CHECK(cpu.anomaly_count == gpu.anomaly_count, "K2 anomaly count equal");
    CHECK(cpu.anomaly_count > 0, "K2 anomalies detected (non-vacuous)");
    if (cpu.anomaly_count == gpu.anomaly_count && cpu.anomaly_count > 0) {
        CHECK(memcmp(cpu.anomaly_records, gpu.anomaly_records,
                     sizeof(uint32_t) * 4 * cpu.anomaly_count) == 0,
              "K2 anomaly records identical (cpu == gpu)");
    }

    /* fixture sanity: every pattern class must appear at least once */
    for (int p = 0; p < N_PATTERNS; ++p) {
        uint32_t hits = 0;
        for (uint32_t i = 0; i < in.line_count; ++i)
            hits += (cpu.line_flags[i] >> p) & 1u;
        char msg[64];
        snprintf(msg, sizeof(msg), "pattern %d exercised (%u hits)", p, hits);
        CHECK(hits > 0, msg);
    }

    sentinel_free_results(&cpu);
    sentinel_free_results(&gpu);
    free(offs);
    free(ips);
    free(buckets);
    free(services);

    printf("== parity %s ==\n", failures ? "FAILED" : "OK");
    return failures ? 1 : 0;
}
