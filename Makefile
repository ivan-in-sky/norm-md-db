# normconv — оркестрация рабочей базы
#
#   make lint                    гейт качества (error: 0)
#   make chunk catalog clauses report   производные артефакты
#   make convert                 пакетная конвертация источников/ -> документы/
#   make convert DOC=<doc_id>    одиночный документ с overrides (правки/)
#
# Полный цикл добавления документа: docs/10-local-agent.md

SHELL      := /bin/bash
MD_DIR     ?= база/документы
SRC_DIR    ?= база/источники
INDEX_DIR  ?= база/индекс
CATALOG    ?= база/каталог
OVERRIDES  ?= база/правки
REGISTRY   ?= база/актуальные-версии.yaml
JOBS       ?= 8
PY         ?= python3
NORMCONV   := $(PY) -m normconv.cli

export PYTHONPATH := tools:$(PYTHONPATH)

.PHONY: help lint chunk catalog clauses report convert clean

help:
	@grep -E '^# ' Makefile | sed 's/^# //'

lint:
	$(NORMCONV) lint $(MD_DIR)

chunk:
	$(NORMCONV) chunk $(MD_DIR) --out $(INDEX_DIR)/chunks.jsonl

catalog:
	$(NORMCONV) catalog $(MD_DIR) --out $(CATALOG)

clauses:
	$(NORMCONV) clauses $(MD_DIR) --out $(CATALOG)/CLAUSES.md

report:
	$(NORMCONV) report $(MD_DIR)

# Одиночный документ: применяет правки/<DOC>.yaml и словарь символов (если есть),
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
	rm -rf $(INDEX_DIR) $(CATALOG) $(MD_DIR)/_reports
