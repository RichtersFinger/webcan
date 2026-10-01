PYTHON_IMAGE := python:3.14.7-alpine@sha256:4677924bcc0e94505a3270e87cb1601c2af54cd92d021b8dc306618a14333bbe
PYTHON := docker run --rm -it -v ./:/webcan -w /webcan ${PYTHON_IMAGE}

shell:
	${PYTHON} sh

test:
	${PYTHON} sh -c "cd .. && python -m unittest discover webcan/tests -t .."

build:
	${PYTHON} sh -c "pip install --uploaded-prior-to P14D 'build==1.6.1' && python -m build --wheel"

clean:
	rm -rf __pycache__ **/__pycache__ webcan.egg-info build dist
