# Java and Maven in a lane

**What hansa does by itself.** Before a lane's setup, check and merge check, and in
front of each Bash command of a worker or verifier, hansa runs a short sh prelude
inside the sandbox, after the lane's env is set:
- The sandbox's proxy wants a user and password, which it puts only in
  `HTTPS_PROXY` and `HTTP_PROXY`; Maven reads neither. When one of them holds a
  proxy, the prelude writes a Maven global settings file in the run's temporary
  folder naming that proxy for https and http (`localhost` and `127.0.0.1` left
  out with `nonProxyHosts`), and adds `-gs <that file>` to `MAVEN_ARGS`, which
  Maven 3.9 reads. A global settings file, so a project's own
  `~/.m2/settings.xml` (its mirrors, say) still applies where the sandbox lets
  Maven read it.
- Java ignores `TMPDIR`, and on macOS the sandbox does not let it write its own
  temporary folder. The prelude adds `-Djava.io.tmpdir=$TMPDIR` to
  `JAVA_TOOL_OPTIONS`, which every JVM reads, the one Surefire starts for the
  tests too.

What was already in `MAVEN_ARGS` and `JAVA_TOOL_OPTIONS` (the sandbox's, or the
lane's env) is kept. The prelude prints nothing, never the proxy's password, and
skips what it cannot do instead of failing the run.

**What this changes for a build script.**
- Every JVM that finds `JAVA_TOOL_OPTIONS` set prints `Picked up
  JAVA_TOOL_OPTIONS: ...` on stderr before anything else. `java -version` and
  `java -XshowSettings` print on stderr, so their output starts with that line;
  `javac -version` and `mvn -v` print on stdout, so theirs starts with it only
  when stderr is read with it (`2>&1`). A script that reads the version from
  the first line (`java -version 2>&1 | sed -n '1s/.*version "\([0-9]*\).*/\1/p'`)
  reads nothing and fails as if java were missing: look for the line holding
  `version "` instead (`sed -n '/version "/{s/.*version "\([0-9]*\).*/\1/p;q;}'`).
- A build script that sets `MAVEN_ARGS` itself drops the `-gs <file>` hansa put
  there, and Maven then gets `407 Proxy Authentication Required` for anything
  its local repository lacks (on a machine whose local repository holds
  everything, nothing shows). In bash, `MAVEN_ARGS=(-B)` makes it an array,
  which is not exported, and `mvn "${MAVEN_ARGS[@]}"` passes only `-B`. Keep
  hansa's: `MAVEN_ARGS=(-B ${MAVEN_ARGS:-})`. A failed step whose output holds
  Maven's 407 (a line with `407` and `proxy authentication` along with Maven's
  own words, such as `Could not transfer artifact`) gets a line saying so.
- On macOS, `mktemp -d`, `mktemp -t PREFIX` and a bare `mktemp` ignore `TMPDIR`:
  they write to the account's own temporary folder under `/var/folders`, which
  a lane may not write (other programs of the account, `hansa serve` among
  them, keep files there), and fail with `mktemp: mkdtemp failed on
  /var/folders/...: Operation not permitted`. Give mktemp a template in
  `TMPDIR`: `mktemp -d "${TMPDIR:-/tmp}/name.XXXXXX"` or `mktemp -d -p
  "$TMPDIR"`. Linux's mktemp reads `TMPDIR`. A failed step whose output holds
  that refusal (`Operation not permitted`, on macOS or under `/var/folders`)
  gets a line saying so; Linux's mktemp refused gets a plainer one, that its
  folder is outside what the lane may write. This holds for any lane, not only Java's.

**What a project should do.** Mockito's inline mock maker attaches itself to the
running JVM, and cannot in the sandbox: the JVM's attach files and socket live in
the system's temporary folder whatever `java.io.tmpdir` says, and the sandbox
does not open that folder to workers. Load Mockito as a Java agent instead, as
Mockito documents; in the pom:
```xml
<properties>
  <argLine></argLine> <!-- empty, so @{argLine} is there without a coverage agent -->
</properties>
...
<plugin>
  <groupId>org.apache.maven.plugins</groupId>
  <artifactId>maven-dependency-plugin</artifactId>
  <executions>
    <execution>
      <goals><goal>properties</goal></goals> <!-- sets ${org.mockito:mockito-core:jar} -->
    </execution>
  </executions>
</plugin>
<plugin>
  <groupId>org.apache.maven.plugins</groupId>
  <artifactId>maven-surefire-plugin</artifactId>
  <configuration>
    <argLine>@{argLine} -javaagent:${org.mockito:mockito-core:jar}</argLine>
  </configuration>
</plugin>
```

**The lane.**
- A Maven repository of the lane's own, on its write list, named in its
  commands: `hansa lane set L write ~/hansa-cache/DESK/L/m2`, and
  `-Dmaven.repo.local=...` with that full path in the setup and check (not
  `~/.m2/repository`, which the write list refuses).
- `repo.maven.apache.org` on the network list, and any other repository the
  build downloads from.
- Tests that bind a local port cannot on macOS unless `localhost` is on the
  network list, which opens the machine's whole loopback. Have such tests skip
  when the bind is refused (JUnit's `Assumptions`, say), as hansa's own do.

Gradle is not covered yet: the prelude sets nothing for it but
`JAVA_TOOL_OPTIONS`.
