# joule:code Java tools – built by joule:code on first use
# (tag joulecode/java-tools:<hash of this directory>). Runs without network.
FROM eclipse-temurin:21-jdk

ARG PMD_VERSION=7.28.0
ARG JAVAPARSER_VERSION=3.28.2

# PMD (findings, dead code) – jar unpacks the zip without further tools.
ADD https://github.com/pmd/pmd/releases/download/pmd_releases%2F${PMD_VERSION}/pmd-dist-${PMD_VERSION}-bin.zip /opt/pmd.zip
RUN cd /opt && jar xf pmd.zip && rm pmd.zip && mv pmd-bin-${PMD_VERSION} pmd && chmod +x pmd/bin/pmd

WORKDIR /opt/joulecode
ADD https://repo1.maven.org/maven2/com/github/javaparser/javaparser-core/${JAVAPARSER_VERSION}/javaparser-core-${JAVAPARSER_VERSION}.jar lib/javaparser-core.jar
COPY Tool.java JfrStats.java ruleset.xml ./
# JfrStats runs in the user's image – therefore compiled for Java 17.
RUN javac -cp lib/javaparser-core.jar -d classes Tool.java \
    && javac --release 17 -d classes JfrStats.java

ENV PATH="/opt/pmd/bin:${PATH}"
