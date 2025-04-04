BLUE='\033[0;34m'
NC='\033[0m' # No Color

test:
	@pytest

lint:
	@echo "\n${BLUE}Running Pylint against source and test files...${NC}\n"
	@pylint ./src
	@echo "\n${BLUE}Running Flake8 against source and test files...${NC}\n"
	@flake8
	@echo "\n${BLUE}Running Bandit against source files...${NC}\n"
	@bandit -c pyproject.toml -r .

clean:
	rm -rf .pytest_cache .coverage .pytest_cache coverage.xml coverage_html_report

.PHONY: clean test