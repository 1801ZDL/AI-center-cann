ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS tools
ARG BASE_PYTHON=/usr/local/bin/python3
COPY requirements-linux-x86_64-py311.txt /opt/ascend-lab-operator/requirements.txt
COPY wheels/ /wheelhouse/
RUN ${BASE_PYTHON} -m venv /opt/ascend-lab-operator/venv && \
    /opt/ascend-lab-operator/venv/bin/python -m pip --isolated install \
    --no-cache-dir --no-index --find-links=/wheelhouse --only-binary=:all: \
    --require-hashes -r /opt/ascend-lab-operator/requirements.txt
COPY app/ /opt/ascend-lab-operator/app/
RUN find /opt/ascend-lab-operator -type d -exec chmod a+rx {} + && \
    find /opt/ascend-lab-operator -type f -exec chmod a+r {} + && \
    chmod -R a-w /opt/ascend-lab-operator

FROM ${BASE_IMAGE}
LABEL io.ascend-lab.component="operator-tools-v1"
COPY --from=tools /opt/ascend-lab-operator /opt/ascend-lab-operator
