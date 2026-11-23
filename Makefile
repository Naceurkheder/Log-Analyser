# Sentinel-GPU — top-level task runner
#
# All local state lives in .env (see .env.example). Copy and edit if needed.

ifneq (,$(wildcard .env))
include .env
export
endif

SHELL      := /bin/bash
FLEET      := ./lxd/fleet.sh
PYTHON     ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
TF_DIR     ?= terraform

.DEFAULT_GOAL := help
.PHONY: help up down status destroy shell dns-check

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ---------------------------------------------------------------------------
# Fleet (LXD)
# ---------------------------------------------------------------------------
up: ## Launch the container fleet (idempotent)
	$(FLEET) up

down: ## Stop the fleet
	$(FLEET) down

status: ## Show fleet status and resource caps
	$(FLEET) status

destroy: ## Delete fleet + network + project (destructive)
	$(FLEET) destroy

shell: ## Open a shell in a container: make shell C=infra
	$(FLEET) shell $(C)

dns-check: ## Verify inter-container name resolution from infra
	$(FLEET) dns-check

# ---------------------------------------------------------------------------
# Dataset (synthetic — replaces bulk LogHub downloads)
# ---------------------------------------------------------------------------
dataset: ## Generate the 100 MB benchmark dataset into data/ (gitignored)
	python3 scripts/gen_dataset.py --size-mb 100 --out data/bench.log --labels-out data/bench.labels.json

dataset-labels: ## Generate a small derived dataset plus its ground-truth manifest
	python3 scripts/gen_dataset.py --lines 20000 --seed 42 --burst-every 80 \
		--out data/ground-truth.log --labels-out data/ground-truth.labels.json

dataset-sample: ## Regenerate the committed test fixture (fixed seed)
	python3 scripts/gen_dataset.py --lines 10000 --out tests/fixtures/sample.log

test: ## Run the python test suite
	$(PYTHON) -m pytest tests/ -q

# ---------------------------------------------------------------------------
# CUDA detector library (Sprint 2)
# ---------------------------------------------------------------------------
NVCC ?= $(shell command -v nvcc 2>/dev/null)
# CUDA 12.4 supports host GCC up to 13.x.  Newer host GCC headers (15.x) are
# rejected by nvcc's front-end, so pick a supported compiler explicitly.
HOST_CXX ?= $(shell for c in g++-13 g++-14 g++; do command -v $$c 2>/dev/null && break; done)
# Static libstdc++ keeps the artifacts runnable inside the older Ubuntu containers.
STATIC_LIBS := -static-libstdc++ -static-libgcc

CXXFLAGS := -O3 -std=c++17 -fPIC -Wall -Wextra
# -U_GNU_SOURCE / -D_DEFAULT_SOURCE: glibc >= 2.41 enables the C23
# cospi()/sinpi()/rsqrt() prototypes under __USE_GNU, and CUDA 12.4's
# <math_functions.h> declares the same names with a different exception
# specification.  Dropping the GNU extensions (keeping the POSIX default set
# that the C++ standard library headers need) sidesteps the clash.
NVCC_FLAGS := -O3 -std=c++17 -arch=sm_86 -ccbin $(HOST_CXX) \
              -Xcompiler=-fPIC,-static-libstdc++,-static-libgcc \
              -U_GNU_SOURCE -D_DEFAULT_SOURCE

BUILD_DIR := build

.PHONY: lib lib-cpu bench-cpu bench-gpu bench-kernels test-cpp clean

lib: $(BUILD_DIR)/libsentinel.so ## Build the CUDA detector library
lib-cpu: $(BUILD_DIR)/libsentinel_cpu.so ## Build the CPU reference library

$(BUILD_DIR)/tokenizer.o: src/common/tokenizer.cpp src/common/tokenizer.h include/sentinel/sentinel.h
	@mkdir -p $(BUILD_DIR)
	$(HOST_CXX) $(CXXFLAGS) -Iinclude -c $< -o $@

$(BUILD_DIR)/detect_cpu.o: src/cpu/detect_cpu.cpp src/common/tokenizer.h include/sentinel/sentinel.h
	@mkdir -p $(BUILD_DIR)
	$(HOST_CXX) $(CXXFLAGS) -Iinclude -Isrc/common -c $< -o $@

$(BUILD_DIR)/detect_gpu.o: src/cuda/detect_gpu.cu src/common/tokenizer.h include/sentinel/sentinel.h
	@mkdir -p $(BUILD_DIR)
	$(NVCC) $(NVCC_FLAGS) -Iinclude -Isrc/common -c $< -o $@

$(BUILD_DIR)/backend_gpu.o: src/backend/backend_gpu.c include/sentinel/sentinel.h
	$(HOST_CXX) $(CXXFLAGS) -Iinclude -c $< -o $@

$(BUILD_DIR)/backend_cpu.o: src/backend/backend_cpu.c include/sentinel/sentinel.h
	$(HOST_CXX) $(CXXFLAGS) -Iinclude -c $< -o $@

$(BUILD_DIR)/libsentinel.so: $(BUILD_DIR)/tokenizer.o $(BUILD_DIR)/detect_gpu.o $(BUILD_DIR)/detect_cpu.o $(BUILD_DIR)/backend_gpu.o
	$(NVCC) $(NVCC_FLAGS) -shared $^ -o $@ -cudart static

$(BUILD_DIR)/libsentinel_cpu.so: $(BUILD_DIR)/tokenizer.o $(BUILD_DIR)/detect_cpu.o $(BUILD_DIR)/backend_cpu.o
	$(HOST_CXX) -shared $^ -o $@

$(BUILD_DIR)/bench_cpu: bench/bench_kernels.cpp $(BUILD_DIR)/tokenizer.o $(BUILD_DIR)/detect_cpu.o
	$(HOST_CXX) $(CXXFLAGS) -Iinclude -DSENTINEL_BACKEND_NAME='"cpu"' -DSENTINEL_DETECT_FN=sentinel_detect_cpu $< $(BUILD_DIR)/tokenizer.o $(BUILD_DIR)/detect_cpu.o -o $@

$(BUILD_DIR)/bench_gpu: bench/bench_kernels.cpp $(BUILD_DIR)/tokenizer.o $(BUILD_DIR)/detect_cpu.o $(BUILD_DIR)/detect_gpu.o
	$(NVCC) $(NVCC_FLAGS) -Iinclude -DSENTINEL_BACKEND_NAME='"gpu"' -DSENTINEL_DETECT_FN=sentinel_detect_gpu $< $(BUILD_DIR)/tokenizer.o $(BUILD_DIR)/detect_cpu.o $(BUILD_DIR)/detect_gpu.o -o $@ -cudart static

bench-cpu: $(BUILD_DIR)/bench_cpu
bench-gpu: $(BUILD_DIR)/bench_gpu

bench-kernels: ## Benchmark 100MB chunk: CPU path vs GPU path (Sprint 2)
	./scripts/run_bench.sh

test-cpp: $(BUILD_DIR)/test_parity ## CPU/GPU parity test on the fixed fixture
	$(BUILD_DIR)/test_parity tests/fixtures/sample.log

$(BUILD_DIR)/test_parity: tests/test_parity.cpp $(BUILD_DIR)/tokenizer.o $(BUILD_DIR)/detect_cpu.o $(BUILD_DIR)/detect_gpu.o
	$(NVCC) $(NVCC_FLAGS) -Iinclude -Isrc $< $(BUILD_DIR)/tokenizer.o $(BUILD_DIR)/detect_cpu.o $(BUILD_DIR)/detect_gpu.o -o $@ -cudart static

clean: ## Remove build artifacts
	rm -rf $(BUILD_DIR)

# ---------------------------------------------------------------------------
# Control API (Sprint 3)
# ---------------------------------------------------------------------------
API_HOST ?= $(if $(SENTINEL_API_HOST),$(SENTINEL_API_HOST),127.0.0.1)
API_PORT ?= $(if $(SENTINEL_API_PORT),$(SENTINEL_API_PORT),8000)

.PHONY: api api-openapi test-api api-bootstrap api-deploy produce test-producer \
	test-cpu cpu-cli cpu-baseline

api: ## Run the control API locally (uvicorn --reload)
	PYTHONPATH=src $(PYTHON) -m sentinel.api.cli serve --host $(API_HOST) --port $(API_PORT) --reload

api-openapi: ## Regenerate the committed OpenAPI document
	PYTHONPATH=src $(PYTHON) -m sentinel.api.cli openapi --out docs/api/openapi.json

test-api: ## Run only the API contract tests
	$(PYTHON) -m pytest tests/api -q

produce: ## Poll the ingest queue once and write chunks/manifests/jobs
	PYTHONPATH=src $(PYTHON) -m sentinel.api.cli produce --once

test-producer: ## Run only the streaming-chunker/producer tests
	$(PYTHON) -m pytest tests/producer tests/unit/test_chunker.py -q

test-cpu: ## Run CPU reference correctness tests
	$(PYTHON) -m pytest tests/cpu -q

cpu-cli: ## Run the CPU reference over the sample fixture
	PYTHONPATH=src $(PYTHON) -m sentinel.detector.cli detect \
		--input tests/fixtures/sample.log --rules tests/fixtures/cpu/rules.json \
		--out results/cpu-reference-sample.json

cpu-baseline: ## Save CPU single-thread (and optional threaded) baselines
	PYTHONPATH=src $(PYTHON) -m sentinel.detector.cli baseline \
		--input data/bench.log --rules tests/fixtures/cpu/rules.json \
		--threads 2 --native

api-bootstrap: ## Provision the API service inside the `api` container (once)
	./lxd/bootstrap-api.sh api

api-deploy: ## Push the API package into `api` and restart the service
	./lxd/deploy-api.sh api

# ---------------------------------------------------------------------------
# Emulated AWS (moto in infra) + Terraform (runs on the host)
# ---------------------------------------------------------------------------
infra: ## Bootstrap emulated AWS + apply Terraform resources (idempotent)
	./lxd/bootstrap-infra.sh infra
	terraform -chdir=$(TF_DIR) init -reconfigure -input=false
	terraform -chdir=$(TF_DIR) apply -auto-approve -input=false

infra-destroy: ## Tear down Terraform-managed emulated AWS resources
	terraform -chdir=$(TF_DIR) destroy -auto-approve -input=false

verify: ## Verify buckets/queues/tables/redrive from the host
	./scripts/verify-infra.sh
