/*
 * bench_kernels.cpp — Sprint 2 benchmark harness.
 *
 * Built twice: --backend=cpu links the sequential reference, --backend=gpu
 * links the CUDA implementation. Methodology notes:
 *
 *   * the file is read fully into memory BEFORE timing (the sprint pitfall:
 *     "don't measure warm page-cache reads") — the timed section is the
 *     detection computation only;
 *   * tokenization is shared preprocessing, also outside the timed section;
 *   * the GPU path timing includes pinned staging + async H2D + kernels +
 *     D2H, i.e. the complete end-to-end detection cost;
 *   * one warm-up pass runs before the measured repetitions.
 */
#include "sentinel/sentinel.h"

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

static const char* DEFAULT_PATTERNS[] = {
    "Failed password",
    "Invalid user",
    "ERROR",
    " 404 ",
    "SEGFAULT",
};

static double now_ms() {
    using clk = std::chrono::steady_clock;
    return std::chrono::duration<double, std::milli>(
               clk::now().time_since_epoch())
        .count();
}

static std::vector<uint8_t> read_file(const char* path) {
    FILE* f = fopen(path, "rb");
    if (!f) {
        fprintf(stderr, "cannot open %s\n", path);
        exit(1);
    }
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    std::vector<uint8_t> buf((size_t)sz);
    if (fread(buf.data(), 1, (size_t)sz, f) != (size_t)sz) {
        fprintf(stderr, "short read on %s\n", path);
        exit(1);
    }
    fclose(f);
    return buf;
}

int main(int argc, char** argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <logfile> [repeats=%d]\n", argv[0], 3);
        return 2;
    }
    int repeats = argc > 2 ? atoi(argv[2]) : 3;
    if (repeats < 1) repeats = 1;

    auto data = read_file(argv[1]);

    sentinel_input in{};
    uint32_t* offs = nullptr;
    uint32_t* ips = nullptr;
    uint16_t* buckets = nullptr;
    uint8_t* services = nullptr;
    int rc = sentinel_tokenize(data.data(), data.size(), &offs, &ips, &buckets,
                               &services, &in.line_count);
    if (rc) {
        fprintf(stderr, "tokenize failed: %d\n", rc);
        return 1;
    }
    in.data = data.data();
    in.data_len = data.size();
    in.line_offsets = offs;
    in.line_ip = ips;
    in.line_bucket = buckets;
    in.line_service = services;

    sentinel_config cfg{};
    for (auto p : DEFAULT_PATTERNS) {
        cfg.patterns[cfg.pattern_count++] = p;
    }
    cfg.z_threshold = 4.0f;

    sentinel_results results{};

    /* warm-up (untimed) */
    rc = SENTINEL_DETECT_FN(&in, &cfg, &results);
    if (rc) {
        fprintf(stderr, "detect failed: %d\n", rc);
        return 1;
    }
    uint32_t flagged_lines = 0, total_hits = 0;
    for (uint32_t i = 0; i < in.line_count; ++i) {
        if (results.line_flags[i]) {
            ++flagged_lines;
            for (uint32_t b = 0; b < cfg.pattern_count; ++b)
                total_hits += (results.line_flags[i] >> b) & 1u;
        }
    }
    uint32_t anomalies = results.anomaly_count;
    sentinel_free_results(&results);

    /* measured repetitions */
    double best = 1e18;
    for (int r = 0; r < repeats; ++r) {
        double t0 = now_ms();
        rc = SENTINEL_DETECT_FN(&in, &cfg, &results);
        double t1 = now_ms();
        if (rc) {
            fprintf(stderr, "detect failed: %d\n", rc);
            return 1;
        }
        sentinel_free_results(&results);
        if (t1 - t0 < best) best = t1 - t0;
    }

    double mb = (double)data.size() / 1e6;
    printf("{\"backend\": \"%s\", \"bytes\": %zu, \"lines\": %u, "
           "\"best_ms\": %.3f, \"mb_per_s\": %.1f, \"lines_per_s\": %.0f, "
           "\"flagged_lines\": %u, \"pattern_hits\": %u, \"anomalies\": %u}\n",
           SENTINEL_BACKEND_NAME, data.size(), in.line_count, best,
           mb / (best / 1000.0),
           (double)in.line_count / (best / 1000.0), flagged_lines, total_hits,
           anomalies);

    free(offs);
    free(ips);
    free(buckets);
    free(services);
    return 0;
}
