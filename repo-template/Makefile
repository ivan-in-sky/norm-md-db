# stroy-norms — orchestration
#
#   make convert JOBS=8      sources/ -> md/
#   make lint                CI gate
#   make chunk catalog clauses   derived artifacts
#   make report              quality dashboard
#   make eval / golden / check   фаза 2 (нужны evals/run_eval.py, tools/tests/)
#   make index / serve-mcp / assets-sync / site   фаза 2 (нужны tools/*.py, site/)
#
# Single document:  make convert DOC=sp-63-13330-2018
# Полный цикл добавления документа: docs/10-local-agent.md

SHELL      := /bin/bash
MD_DIR     ?= md
SRC_DIR    ?= sources
INDEX_DIR  ?= index
CATALOG    ?= catalog
JOBS       ?= 8
PY         ?= python3
NORMCONV   := $(PY) -m normconv.cli

export PYTHONPATH := tools:$(PYTHONPATH)

.PHONY: all help install convert lint chunk catalog report eval golden check \
        site serve-mcp assets-sync clean dist

all: convert lint chunk catalog clauses report

help:
	@grep -E '^# ' Makefile | sed 's/^# //'

install:
	$(PY) -m pip install -e "tools/[dev]"

# ---- conversion -----------------------------------------------------------
# Одиночный документ: применяет overrides/<DOC>.yaml и словарь символов
# (если есть), всегда --force (override-файлы не входят в hash кэша).
# Пакетный режим конвертирует без per-doc overrides — документы с правками
# пересобираются поштучно (make convert DOC=<doc_id>).
LICENSE  ?= официальный документ (ГК РФ ст.1259 п.6)
REGISTRY ?= актуальные-версии.yaml
convert:
ifdef DOC
	$(NORMCONV) convert $(shell ls $(SRC_DIR)/**/$(DOC).* 2>/dev/null || echo $(SRC_DIR)/$(DOC).docx) \
	    --out $(MD_DIR) --jobs 1 --force --registry $(REGISTRY) --license "$(LICENSE)" \
	    $(if $(wildcard overrides/$(DOC).yaml),--meta-override overrides/$(DOC).yaml) \
	    $(if $(wildcard overrides/symbols/$(DOC).yaml),--glyphs overrides/symbols/$(DOC).yaml)
else
	$(NORMCONV) convert $(SRC_DIR) --out $(MD_DIR) --jobs $(JOBS) \
	    --registry $(REGISTRY) --license "$(LICENSE)"
endif

report:
	$(NORMCONV) report $(MD_DIR)

# ---- quality gates --------------------------------------------------------
lint:
	$(NORMCONV) lint $(MD_DIR)

golden:
	$(PY) tools/tests/run_golden.py evals/golden --md $(MD_DIR)

eval:
	$(PY) evals/run_eval.py evals/qa.yaml --index $(INDEX_DIR)/chunks.jsonl

check: lint golden eval

# ---- derived artifacts ----------------------------------------------------
chunk:
	$(NORMCONV) chunk $(MD_DIR) --out $(INDEX_DIR)/chunks.jsonl

catalog:
	$(NORMCONV) catalog $(MD_DIR) --out $(CATALOG)

# указатель пунктов: block id -> номер строки -> ссылки для GitHub и Obsidian
REPO_URL ?= https://github.com/<org>/stroy-norms
REF      ?= main
clauses:
	$(NORMCONV) clauses $(MD_DIR) --out $(CATALOG)/CLAUSES.md --repo $(REPO_URL) --ref $(REF)

index: chunk
	$(PY) tools/index_build.py --chunks $(INDEX_DIR)/chunks.jsonl --backend qdrant

assets-sync:
	$(PY) tools/assets_sync.py --md $(MD_DIR) --remote $${NORMS_ASSETS_URL}

site:
	mkdocs build -f site/mkdocs.yml

serve-mcp:
	$(PY) tools/serve_mcp.py --index $(INDEX_DIR)/chunks.jsonl --catalog $(CATALOG)/catalog.jsonl

dist:
	mkdir -p dist && tar --zstd -cf dist/stroy-norms-md-$$(date +%F).tar.zst $(MD_DIR) $(CATALOG)

clean:
	rm -rf $(INDEX_DIR) md/_reports dist .normconv-state.json
