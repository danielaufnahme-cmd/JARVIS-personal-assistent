## Rust or Go for a small CLI tool

**Short answer:** pick **Go** if you want it done this weekend, **Rust** if the tool will live for years and
handle untrusted input.

### How they compare
| | Rust | Go |
|---|---|---|
| Binary size | ~1–4 MB | ~2–8 MB |
| Build time | slow (minutes) | fast (seconds) |
| Error handling | `Result<T, E>`, `?` | `if err != nil` |
| Learning curve | steep | gentle |

### Where Rust wins
- **Correctness:** the borrow checker rules out data races and most null/ownership bugs at compile time.
- **clap + serde** make argument parsing and config files almost declarative.
- Startup is instant and memory use is predictable, which matters for tools run thousands of times in scripts.

### Where Go wins
- You will be productive in an afternoon; the standard library covers HTTP, JSON and files.
- Cross-compiling is one environment variable: `GOOS=windows go build`.
- Concurrency with goroutines is simpler to reason about for I/O-bound tools.

![benchmark chart](https://example.com/chart.png)

### A minimal Rust starting point
```rust
use clap::Parser;

#[derive(Parser)]
struct Args {
    /// Files to process
    paths: Vec<std::path::PathBuf>,
}

fn main() -> anyhow::Result<()> {
    let args = Args::parse();
    for p in &args.paths {
        println!("{}", p.display());
    }
    Ok(())
}
```

### Recommendation
1. Prototype the tool in Go to pin down what it needs to do.
2. If it grows beyond ~2,000 lines or starts parsing untrusted files, port the core to Rust.
3. Either way, keep the CLI surface small and write the integration tests first.

> Rule of thumb: the language matters less than a clear, small command-line interface.
