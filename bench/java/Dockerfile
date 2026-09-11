# Build context is the monorepo root — see ../../scripts/build_and_run.sh.
#
# seda-bus-java's pom.xml declares a dependency on resolvingarchitecture:
# common:1.2.0, but ra-common-java's own pom.xml is currently at 1.3.2 - a
# real version-skew finding from building this benchmark, noted in
# ../../METHODOLOGY.md. Rather than edit either repo to paper over it, this
# installs ra-common-java's build output into the local Maven repo under
# BOTH its real version and the 1.2.0 alias seda-bus-java's pom still asks
# for, so the existing poms resolve unmodified.
FROM maven:3.9-eclipse-temurin-21 AS build
WORKDIR /src
COPY common/ra-common-java/ common/ra-common-java/
COPY seda-bus/seda-bus-java/ seda-bus/seda-bus-java/
COPY seda-bus/seda-bus-compare/bench/java/ seda-bus/seda-bus-compare/bench/java/

WORKDIR /src/common/ra-common-java
RUN mvn -q -DskipTests install \
    && mvn -q org.apache.maven.plugins:maven-install-plugin:3.1.2:install-file \
       -Dfile=target/ra-common-1.3.2.jar -DgroupId=resolvingarchitecture \
       -DartifactId=common -Dversion=1.2.0 -Dpackaging=jar

WORKDIR /src/seda-bus/seda-bus-java
RUN mvn -q -DskipTests install

WORKDIR /src/seda-bus/seda-bus-compare/bench/java
RUN mvn -q package

FROM eclipse-temurin:21-jre AS run
COPY --from=build /src/seda-bus/seda-bus-compare/bench/java/target/bench-jar-with-dependencies.jar /bench.jar
ENTRYPOINT ["java", "-jar", "/bench.jar"]
