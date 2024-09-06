#!/bin/bash

set +x
. src/.evergreen/scripts/env.sh
for i in $(find $DRIVERS_TOOLS/.evergreen $PROJECT_DIRECTORY/.evergreen -name \*.sh); do
    cat $i | tr -d '\r' >$i.new
    mv $i.new $i
done
# Copy client certificate because symlinks do not work on Windows.
cp $DRIVERS_TOOLS/.evergreen/x509gen/client.pem $MONGO_ORCHESTRATION_HOME/lib/client.pem