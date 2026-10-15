PLUGIN_DIR ?= $(HOME)/.config/omarchy/plugins/mylonz.hue

.PHONY: help deploy watch validate install enable disable status

help:
	@echo "make deploy   - copy into $(PLUGIN_DIR) and reload the shell"
	@echo "make watch    - deploy on every save (Ctrl-C to stop)"
	@echo "make validate - run omarchy's manifest validator on the deployed copy"
	@echo "make install  - deploy, then build the venv and CLI shims"
	@echo "make enable   - deploy, then enable the bar widget"
	@echo "make disable  - remove the widget from the bar"
	@echo "make status   - show the plugin's registration"

deploy:
	./deploy.sh

watch:
	./watch.sh

validate:
	omarchy plugin validate $(PLUGIN_DIR)

install: deploy
	$(PLUGIN_DIR)/install.sh

enable: deploy
	omarchy plugin enable mylonz.hue

disable:
	omarchy plugin disable mylonz.hue

status:
	@omarchy-shell shell listPlugins | jq '.[] | select(.id=="mylonz.hue")'
