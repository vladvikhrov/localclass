# Автоматизация разработки на Linux. Windows-сборка — scripts/build_windows.ps1 или GitHub Actions.
PY := .venv/bin/python
.PHONY: venv test test-fast gui node cluster cluster-gui clean lint release

venv:            ## создать окружение и поставить зависимости
	python3 -m venv .venv
	$(PY) -m pip install --upgrade pip -q
	$(PY) -m pip install -e ".[dev]" -q

test:            ## все тесты (fault-injection сценарии ТЗ 15.3 включительно)
	$(PY) -m pytest -q

test-fast:       ## только юнит-тесты
	$(PY) -m pytest -q tests/test_protocol.py tests/test_store.py

check-qt:        ## системные библиотеки Qt для Linux (на Windows не нужно)
	@ldconfig -p | grep -q libxcb-cursor || { echo "Нет libxcb-cursor0 — Qt не покажет окно. Установите: sudo apt-get install -y libxcb-cursor0 tmux"; exit 1; }

gui: check-qt    ## GUI одного узла (dev-каталог run/GUI)
	$(PY) -m localclass --device GUI --port 5100 --data run/GUI

node:            ## headless-узел с консолью
	$(PY) -m localclass.node --device N --port 5200 --data run/N

cluster:         ## 3 headless-узла в tmux
	scripts/run_cluster.sh 3

cluster-gui: check-qt ## 3 GUI-узла на одном ПК
	scripts/run_cluster.sh 3 --gui

release:         ## отправить тег vX.Y.Z → GitHub Actions соберёт exe и установщик
	@v=$$($(PY) -c "import localclass;print(localclass.__version__)"); git tag -a "v$$v" -m "LocalClass $$v" && git push origin "v$$v" && echo "тег v$$v отправлен — см. вкладку Actions/Releases"

clean:
	rm -rf run build dist .pytest_cache; find . -name __pycache__ -type d -exec rm -rf {} +
