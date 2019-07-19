SHELL = /bin/bash

BOLD=\033[1m
NORMAL=\033[0m

VER := $(shell cat skyportal/__init__.py | grep "__version__ =" | cut -d "'" -f 2)
BANNER := $(shell echo -e "Welcome to $(BOLD)SkyPortal v$(VER)$(NORMAL) (https://skyportal.io)")

$(info $())
$(info $(BANNER))
$(info $())

help:
	@echo -e "  To $(BOLD)start$(NORMAL) the web application, do \`make run\`."
	@echo -e "  To $(BOLD)customize$(NORMAL) the configuration, edit \`config.yaml.defaults\`."
	@echo
	@echo Please choose one of the following make targets:
	@python baselayer/tools/makefile_to_help.py "Web Server":baselayer/Makefile "SkyPortal-specific":Makefile
	@echo

baselayer/Makefile:
	git submodule update --init --remote

docker-images: ## Make and upload docker images
docker-images: docker-local
	@# Add --no-cache flag to rebuild from scratch
	cd baselayer && git submodule update --init --remote
	docker build -t skyportal/web . && docker push skyportal/web

docker-local: ## Build docker images locally
	@echo "!! WARNING !! The current directory will be bundled inside of"
	@echo "              the Docker image.  Make sure you have no passwords"
	@echo "              or tokens in configuration files before continuing!"
	@echo
	@echo "Press enter to confirm that you want to continue."
	@read
	cd baselayer && git submodule update --init --remote
	docker build -t skyportal/web .

db_init: 
	@$(MAKE) --no-print-directory -C . -f baselayer/Makefile $@
	@PYTHONPATH=. tools/db_init.py

doc_reqs:
	pip install -q -r requirements.docs.txt

api-docs: | doc_reqs
	@PYTHONPATH=. python tools/openapi/build-spec.py
	npx redoc-cli@0.8.3 bundle openapi.json --title "SkyPortal API docs" --cdn
	rm -f openapi.{yml,json}
	mkdir -p doc/_build/html
	mv redoc-static.html doc/openapi.html

docs: ## Build the SkyPortal docs
docs: | doc_reqs api-docs
	export SPHINXOPTS=-W; make -C doc html

load_demo_data: ## Import example dataset
load_demo_data: | dependencies
	@PYTHONPATH=. python tools/load_demo_data.py

# https://www.gnu.org/software/make/manual/html_node/Overriding-Makefiles.html
%: baselayer/Makefile force
	@$(MAKE) --no-print-directory -C . -f baselayer/Makefile $@

.PHONY: Makefile force
