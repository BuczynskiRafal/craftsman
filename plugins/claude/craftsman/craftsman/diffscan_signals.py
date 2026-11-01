"""Content risk-signal regex catalog for `craftsman.diffscan`.

Each pattern detects a domain risk signal in added or removed diff lines so review lens selection
can activate the matching specialist lenses (security, concurrency, error handling, data model,
platform portability, etc.). All patterns use `re.VERBOSE`; whitespace and `#` comments are
ignored outside character classes.
"""

from __future__ import annotations

import re

_AUTH_WORDS = (
    "password", "passwd", "token", "secret", "api_key", "apikey", "credential", "permission",
    "jwt", "cookie", "session", "csrf", "oauth", "auth", "authn", "authz", "authenticate",
    "authenticated", "authentication", "authorize", "authorized", "authorization",
)  # fmt: skip
_AUTH_ALTERNATION = "|".join(sorted(_AUTH_WORDS, key=len, reverse=True))
_AUTH_CAMEL_ALTERNATION = "|".join(
    word.capitalize() for word in sorted(_AUTH_WORDS, key=len, reverse=True) if "_" not in word
)

CONTENT_SIGNAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "process": re.compile(
        r"""
        \bsubprocess\.                                              # Python subprocess module
        | \bos\.(?:system|popen|exec\w*|spawn\w*)\s*\(              # Python os process spawners
        | \bshell\s*=\s*True\b                                      # Python subprocess shell=True
        | \bchild_process\b                                         # Node.js child_process module
        | \bexec\.Command(?:Context)?\s*\(                          # Go os/exec Command / CommandContext
        | Runtime\.getRuntime\(\)\.exec                             # Java Runtime.exec
        | \bProcess\.Start\s*\(                                     # .NET System.Diagnostics.Process.Start
        | \bProcessBuilder\b                                        # Java ProcessBuilder
        | \b(?:execSync|spawnSync|execFile)\b                       # Node.js child_process sync/file helpers
        | (?<![\w.])(?:std::)?(?:system|popen|exec(?:l|lp|le|v|ve|vp|vpe))\s*\(  # C/C++ libc / POSIX exec family
        | \bCreateProcess[AW]?\s*\(                                 # Win32 CreateProcess API
        | \bShellExecute(?:Ex)?[AW]?\s*\(                           # Win32 ShellExecute API
        | ["'\]]\s*\.execute\s*\(                                   # Groovy string/list .execute()
        | (?<![\w.$])(?:sh|bat|powershell|pwsh)\s*[("']             # Jenkins pipeline shell steps
        """,
        re.VERBOSE,
    ),
    "filesystem": re.compile(
        r"""
        (?<![\w])open\s*\(                                          # Python / C-like bare open(...)
        | \bPath\s*\(                                               # Python pathlib.Path(...)
        | \bos\.path\.                                              # Python os.path.*
        | \bshutil\.                                                # Python shutil.*
        | \bos\.(?:remove|unlink|rename|replace|makedirs|mkdir|rmdir|chmod|listdir|scandir|walk)\s*\(  # Python os filesystem mutations/walks
        | \bunlink\s*\(                                             # POSIX / C / Python Path.unlink(...)
        | \brmtree\b                                                # Python shutil.rmtree
        | \bfs\.\w                                                  # Node.js fs.*
        | \bFiles?\.\w                                              # Java java.io.File / java.nio.file.Files
        | \btempfile\.                                              # Python tempfile.*
        | \.(?:read_text|write_text|read_bytes|write_bytes|touch|mkdir|rmdir)\s*\(  # Python pathlib.Path I/O methods
        | \bos\.(?:Open|Create|ReadFile|WriteFile|Remove|RemoveAll|MkdirAll)\b      # Go os file helpers
        | \bioutil\.                                                # Go io/ioutil.*
        | \bfopen\s*\(                                              # C stdio fopen(...)
        | \bstd::(?:filesystem|[io]?fstream)\b                      # C++ std::filesystem and file streams
        | \bnew\s+File\s*\(                                         # Java / Groovy new File(...)
        | \b(?:readFile|writeFile|deleteDir)\b                      # Node.js / Jenkins pipeline file steps
        """,
        re.VERBOSE,
    ),
    "network": re.compile(
        r"""
        \brequests\.                                                # Python requests.*
        | \bhttpx\.                                                 # Python httpx.*
        | \burllib\b                                                # Python urllib
        | \bfetch\s*\(                                              # Browser / Node.js fetch(...)
        | \baxios\b                                                 # JavaScript axios
        | \bsocket\.                                                # Python / POSIX socket.*
        | \baiohttp\b                                               # Python aiohttp
        | \bhttp\.(?:Client|Get|Post|NewRequest)\b                  # Go net/http client APIs
        | \bHttpClient\b                                            # Java / .NET HttpClient
        | \bWebSocket\b                                             # WebSocket client/server types
        | \burlopen\s*\(                                            # Python urllib.request.urlopen(...)
        | \bcurl_easy_\w+                                           # C libcurl easy interface
        | \bboost::asio\b                                           # C++ Boost.Asio networking
        | \bhttpRequest\b                                           # Jenkins HTTP Request plugin step
        """,
        re.VERBOSE,
    ),
    "sql": re.compile(
        r"""
        (?i:
            \bselect\b.+?\bfrom\b                                   # SQL SELECT ... FROM
            | \binsert\s+into\b                                     # SQL INSERT INTO
            | \bupdate\s+\w+\s+set\b                                # SQL UPDATE <table> SET
            | \bdelete\s+from\b                                     # SQL DELETE FROM
        )
        | \.execute(?:many)?\s*\((?!\s*\))                          # DB-API .execute(...) / .executemany(...) with arguments
        | \bcursor\s*[.(]                                           # DB cursor variable or constructor
        | \.cursor\s*\(                                             # DB connection .cursor()
        | \.query\s*\(                                              # ORM / JS / Go DB .query(...)
        """,
        re.VERBOSE,
    ),
    "deserialization": re.compile(
        r"""
        \bpickle\.                                                  # Python pickle.*
        | \byaml\.(?:load|unsafe_load|full_load)\s*\(               # PyYAML non-safe loaders
        | \bmarshal\.                                               # Python marshal.*
        | \bshelve\.                                                # Python shelve.*
        | \bjsonpickle\b                                            # Python jsonpickle
        | (?<![\w.])(?:eval|exec)\s*\(                              # Python / JS dynamic code eval/exec
        | \bJSON\.parse\s*\(                                        # JavaScript JSON.parse(...)
        | \bObjectInputStream\b                                     # Java native serialization stream
        | \bevaluate\s*\(\s*(?:new\s+File|readFile)\b               # Groovy / Jenkins dynamic script evaluation
        | \bGroovyShell\b                                           # Groovy GroovyShell interpreter
        | \bEval\.(?:me|x|xy|xyz)\s*\(                              # Groovy Eval.* helpers
        """,
        re.VERBOSE,
    ),
    "concurrency": re.compile(
        r"""
        \bthreading\b                                               # Python threading module
        | \bThread\s*\(                                             # Python / Java Thread(...)
        | \bR?Lock\s*\(                                             # Python Lock() / RLock()
        | \bSemaphore\s*\(                                          # Python / Java Semaphore(...)
        | \basyncio\b                                               # Python asyncio module
        | \bawait\s                                                 # Python / JS / C# / Rust await expression
        | \basync\s+def\b                                           # Python async function definition
        | \bPromise\b                                               # JavaScript Promise
        | \bgo\s+func\b                                             # Go anonymous goroutine launch
        | \bsync\.(?:Mutex|RWMutex|WaitGroup|Once)\b                # Go sync primitives
        | \bmultiprocessing\b                                       # Python multiprocessing module
        | \bconcurrent\.futures\b                                   # Python concurrent.futures executors
        | \batomic\b                                                # C++ / Rust / Go atomic primitives
        | \bAtomic[A-Z]\w*                                          # Java java.util.concurrent.atomic.Atomic*
        | \bsynchronized\b                                          # Java synchronized keyword
        | \bstd::(?:j?thread|(?:shared_|recursive_)?mutex|condition_variable|async|future|promise|lock_guard|unique_lock|scoped_lock)\b  # C++ std concurrency types
        | \bpthread_\w+                                             # POSIX pthreads API
        | (?<![\w.])parallel\s*[({]                                 # Jenkins / DSL parallel stages
        """,
        re.VERBOSE,
    ),
    "time": re.compile(
        r"""
        \bdatetime\b                                                # Python datetime module/types
        | \btime\.(?:time|sleep|monotonic|perf_counter|strftime|localtime|gmtime)\s*\(  # Python time clocks and sleepers
        | \btimezone\b                                              # Python datetime.timezone
        | \btzinfo\b                                                # Python tzinfo
        | \bzoneinfo\b                                              # Python zoneinfo
        | \bpytz\b                                                  # Python pytz
        | \bnew\s+Date\s*\(                                         # JavaScript / Java new Date(...)
        | \bDate\.now\s*\(                                          # JavaScript Date.now()
        | \butcnow\b                                                # Python datetime.utcnow
        | \btime\.(?:Now|Since|Sleep|Duration)\b                    # Go time package clocks/durations
        | \bstd::chrono\b                                           # C++ std::chrono
        | (?<![\w.])(?:localtime|gmtime|mktime|strftime)(?:_[rs])?\s*\(  # C <time.h> calendar conversions
        """,
        re.VERBOSE,
    ),
    "regex": re.compile(
        r"""
        \bre\.(?:compile|match|search|sub|subn|fullmatch|findall|finditer|split)\s*\(  # Python re module entrypoints
        | \bnew\s+RegExp\s*\(                                       # JavaScript new RegExp(...)
        | \bregexp\.(?:MustCompile|Compile|MatchString)\s*\(        # Go regexp package
        | \bRegex\s*\(                                              # .NET / Kotlin / Scala Regex(...)
        | \bPattern\.compile\s*\(                                   # Java java.util.regex.Pattern.compile
        | \b(?:std|boost)::regex\b                                  # C++ std::regex / boost::regex
        | ==?~                                                      # Perl / Ruby / Groovy / Bash regex match operators (=~, ==~)
        """,
        re.VERBOSE,
    ),
    "auth": re.compile(
        rf"""
        (?<![A-Za-z])(?i:{_AUTH_ALTERNATION})s?(?![a-z])            # standalone or snake_case auth/secret terms (optional plural)
        | (?<=[a-z0-9])(?:{_AUTH_CAMEL_ALTERNATION})s?(?![a-z])     # camelCase suffix auth/secret terms (e.g. accessToken, apiKey)
        """,
        re.VERBOSE,
    ),
    "errors": re.compile(
        r"""
        (?m:^\s*try\s*: | ^\s*finally\s*:)                          # Python try: / finally: block headers
        | \bexcept\b                                                # Python except clause
        | \braise\b                                                 # Python raise statement
        | \bcatch\s*[({]                                            # JS / Java / C++ / Go-like catch(...) / catch {
        | \bthrows?\b                                               # JS / Java / C++ throw / throws
        | \bpanic\s*\(                                              # Go / Rust-like panic(...)
        | \brecover\s*\(                                            # Go recover()
        | \bResult<                                                 # Rust / Swift / Kotlin generic Result<...>
        | \.unwrap\(\)                                              # Rust Option/Result .unwrap()
        | \.expect\(                                                # Rust Option/Result .expect(...)
        | \bif\s+err\s*!=\s*nil\b                                   # Go idiomatic error check
        """,
        re.VERBOSE,
    ),
    "datamodel": re.compile(
        r"""
        \bclass\s+[A-Za-z_]                                         # class declaration
        | @dataclass\b                                              # Python @dataclass decorator
        | \bTypedDict\b                                             # Python typing.TypedDict
        | \bNamedTuple\b                                            # Python typing.NamedTuple
        | \bBaseModel\b                                             # Pydantic BaseModel
        | \bProtocol\b                                              # Python typing.Protocol
        | \bEnum\b                                                  # Python enum.Enum
        | \binterface\s+[A-Za-z_]                                   # TypeScript / Java / Go interface declaration
        | (?m:^\s*(?:export\s+)?type\s+\w+(?:<[^>]*>)?\s*=)         # TypeScript / Rust / Go type alias
        | \bstruct\s+[A-Za-z_]                                      # C / C++ / Rust / Go named struct
        | \bstruct\s*\{                                             # Go / C anonymous struct { ... }
        | \benum\s+[A-Za-z_]                                        # C / C++ / Rust / Java / TS enum declaration
        | \brecord\s+[A-Z]                                          # Java / C# record declaration
        """,
        re.VERBOSE,
    ),
    "platform": re.compile(
        r"""
        \bsys\.platform\b                                           # Python sys.platform
        | \bos\.(?:name|sep|pathsep|linesep)\b                      # Python os portability attributes
        | \bplatform\.system\s*\(                                   # Python platform.system()
        | \bwin32\b                                                 # Windows platform identifier
        | \\r\\n                                                    # explicit CRLF escape sequence in source
        | \bencoding\s*=                                            # Python text encoding=... keyword argument
        | \.encode\s*\(                                             # string .encode(...)
        | \.decode\s*\(                                             # bytes .decode(...)
        | \blocale\b                                                # locale-dependent formatting/encoding
        | (?i:\bpowershell\b | \bcmd\.exe\b)                        # Windows shells (PowerShell, cmd.exe)
        | \bAPPDATA\b                                               # Windows APPDATA environment variable
        | \bUSERPROFILE\b                                           # Windows USERPROFILE environment variable
        | \bHOME\b                                                  # POSIX HOME environment variable
        | \bruntime\.GOOS\b                                         # Go target OS constant
        | \bprocess\.platform\b                                     # Node.js process.platform
        """,
        re.VERBOSE,
    ),
}
