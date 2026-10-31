# norm-md-db — оркестрация базы нормативов
#
#   make lint                    гейт качества (error: 0)
#   make chunk catalog clauses report   производные артефакты
#   make convert                 пакетная конвертация .sources/ -> data/
#   make convert DOC=<doc_id>    одиночный документ с overrides (tools/config/)
#
# Полный цикл добавления документа: .dev/runbook.md

SHELL       := /bin/bash
MD_DIR      ?= data
SRC_DIR     ?= .sources
INDEX_DIR   ?= .tmp/index
CATALOG_DIR ?= .tmp/catalog
OVERRIDES   ?= tools/config
REGISTRY    ?= data/registry.yaml
JOBS        ?= 8
PY          ?= python3
NORMCONV    := $(PY) -m normconv.cli

export PYTHONPATH := tools:$(PYTHONPATH)

.PHONY: help lint chunk catalog clauses report convert clean

help:
	@grep -E '^# ' Makefile | sed 's/^# //'

lint:
	$(NORMCONV) lint $(MD_DIR)

chunk:
	mkdir -p $(INDEX_DIR)
	$(NORMCONV) chunk $(MD_DIR) --out $(INDEX_DIR)/chunks.jsonl

catalog:
	mkdir -p $(CATALOG_DIR)
	$(NORMCONV) catalog $(MD_DIR) --out $(CATALOG_DIR)
	sed 's|](\.\./|](|g' $(CATALOG_DIR)/INDEX.md > CATALOG.md

clauses:
	$(NORMCONV) clauses $(MD_DIR) --out $(CATALOG_DIR)/CLAUSES.md

report:
	$(NORMCONV) report $(MD_DIR)

# Одиночный документ: применяет tools/config/<DOC>.yaml и словарь символов (если есть),
# всегда --force (override-файлы не входят в hash кэша).
LICENSE  ?= официальный документ (ГК РФ ст.1259 п.6)
convert:
ifdef DOC
	$(NORMCONV) convert $(shell ls $(SRC_DIR)/**/$(DOC).* 2>/dev/null || echo $(SRC_DIR)/$(DOC).docx) \
	    --out $(MD_DIR) --jobs 1 --force --registry $(REGISTRY) --license "$(LICENSE)" \
	    $(if $(wildcard $(OVERRIDES)/$(DOC).yaml),--meta-override $(OVERRIDES)/$(DOC).yaml) \
	    $(if $(wildcard $(OVERRIDES)/символы-$(DOC).yaml),--glyphs $(OVERRIDES)/символы-$(DOC).yaml)
else
	$(NORMCONV) convert $(SRC_DIR) --out $(MD_DIR) --jobs $(JOBS) \
	    --registry $(REGISTRY) --license "$(LICENSE)"
endif

clean:
	rm -rf $(INDEX_DIR) $(CATALOG_DIR) CATALOG.md data/**/_reports
