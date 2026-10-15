FROM eclipse-temurin:21-jdk
# Fetch dependencies at build time – containers have no network at run time.
ARG JUNIT_VERSION=6.0.3
ADD https://repo1.maven.org/maven2/org/junit/platform/junit-platform-console-standalone/${JUNIT_VERSION}/junit-platform-console-standalone-${JUNIT_VERSION}.jar /opt/junit/junit.jar
WORKDIR /app
COPY . /app
# Compile at build time: a compiler error in a candidate counts like a failing test.
RUN javac -d out/main $(find src/main/java -name '*.java') \
    && javac -cp out/main:/opt/junit/junit.jar -d out/test $(find src/test/java -name '*.java') \
    && javac -cp out/main -d out/bench bench/Run.java
