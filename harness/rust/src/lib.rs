use anyhow::{Context, Result};
use clap::Parser;
use std::fs;
use std::path::PathBuf;
use std::str::FromStr;

#[cfg(feature = "mimalloc")]
use mimalloc::MiMalloc;

#[cfg(feature = "mimalloc")]
#[global_allocator]
static GLOBAL: MiMalloc = MiMalloc;

/// Parse a `--param key=value` argument. Splits on the first `=` only, so values
/// may themselves contain `=`. The error type is `String`, which clap accepts
/// (`String: Into<Box<dyn Error + Send + Sync>>`).
fn parse_kv(s: &str) -> Result<(String, String), String> {
    match s.split_once('=') {
        Some((k, v)) if !k.is_empty() => Ok((k.to_string(), v.to_string())),
        _ => Err(format!("invalid --param (expected key=value): {s}")),
    }
}

#[derive(Parser, Debug)]
#[command(author, version, about, long_about = None)]
pub struct Args {
    #[arg(long)]
    pub input: PathBuf,

    #[arg(long)]
    pub output: PathBuf,

    /// Generic encoder/decoder tunable as `key=value`. Repeatable; the last
    /// occurrence of a key wins. Which keys an implementation honours depends on
    /// the implementation (see its schema in `bench_lib/models.py`). Unknown
    /// keys are ignored, so the orchestrator can pass a superset safely.
    #[arg(long = "param", value_parser = parse_kv)]
    pub params: Vec<(String, String)>,

    /// DEPRECATED back-compat shim: a named quality preset that is folded into
    /// `params` as `quality-tier=<value>` (unless `quality-tier` is given
    /// explicitly). Removed once the orchestrator emits `--param` directly.
    #[arg(long)]
    pub quality: Option<String>,

    #[arg(long, default_value_t = 10)]
    pub iterations: u32,

    #[arg(long, default_value_t = 2)]
    pub warmup: u32,

    #[arg(long, default_value_t = 0)]
    pub threads: usize,

    #[arg(long)]
    pub discard: bool,
}

impl Args {
    /// Look up a tunable by key. The last occurrence wins (last-write semantics
    /// for a repeated `--param key=...`).
    pub fn param(&self, key: &str) -> Option<&str> {
        self.params
            .iter()
            .rev()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v.as_str())
    }

    /// Tunable as a string, or `default` if absent.
    pub fn param_str(&self, key: &str, default: &str) -> String {
        self.param(key).unwrap_or(default).to_string()
    }

    /// Tunable parsed via `FromStr`, falling back to `default` if absent or
    /// unparseable (a warning is printed to stderr in the latter case).
    pub fn param_parsed<T: FromStr>(&self, key: &str, default: T) -> T {
        match self.param(key) {
            Some(raw) => raw.parse().unwrap_or_else(|_| {
                eprintln!("warning: could not parse --param {key}={raw}; using default");
                default
            }),
            None => default,
        }
    }

    /// Tunable as `u32`, or `default` if absent/unparseable.
    pub fn param_u32(&self, key: &str, default: u32) -> u32 {
        self.param_parsed(key, default)
    }

    /// Tunable as `f32`, or `default` if absent/unparseable.
    pub fn param_f32(&self, key: &str, default: f32) -> f32 {
        self.param_parsed(key, default)
    }

    /// Tunable as a boolean. Present-but-non-truthy values are `false`; absent
    /// keys return `default`. Truthy values: `1`, `true`, `yes`, `on`.
    pub fn param_bool(&self, key: &str, default: bool) -> bool {
        match self.param(key) {
            Some(raw) => matches!(
                raw.to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            ),
            None => default,
        }
    }
}

pub trait BenchmarkImplementation {
    fn name(&self) -> &'static str;

    /// Called once before the loop to prepare any resources (e.g. loading the image/data)
    /// Returns a context object that is passed to each iteration.
    fn prepare(&self, args: &Args) -> Result<Box<dyn std::any::Any>>;

    /// The core operation to benchmark (encode or decode).
    /// `context` is the object returned by `prepare`.
    /// Should return the output bytes.
    fn run(&self, args: &Args, context: &mut dyn std::any::Any) -> Result<Vec<u8>>;
}

pub fn main<I: BenchmarkImplementation>(impl_: I) -> Result<()> {
    let mut args = Args::parse();

    // Back-compat shim: fold the legacy `--quality <tier>` preset into `params`
    // so implementations only ever read from `params`. Removed once the
    // orchestrator emits `--param quality-tier=<tier>` directly.
    if let Some(tier) = args.quality.clone() {
        if args.param("quality-tier").is_none() {
            args.params.push(("quality-tier".to_string(), tier));
        }
    }

    // Set thread count environment variables before any threads are spawned.
    if args.threads > 0 {
        // SAFETY: called at program start before any threads are spawned.
        unsafe {
            std::env::set_var("RAYON_NUM_THREADS", args.threads.to_string());
            std::env::set_var("OMP_NUM_THREADS", args.threads.to_string());
        }
    }

    let mut context = impl_
        .prepare(&args)
        .context("Failed to prepare benchmark")?;

    // Warmup
    for _ in 0..args.warmup {
        let _ = impl_
            .run(&args, context.as_mut())
            .context("Warmup iteration failed")?;
    }

    // Measurement loop
    for _ in 0..args.iterations {
        let output = impl_
            .run(&args, context.as_mut())
            .context("Benchmark iteration failed")?;

        if args.discard {
            let mut hasher = crc32fast::Hasher::new();
            hasher.update(&output);
            let checksum = hasher.finalize();
            // Prevent optimization
            std::hint::black_box(checksum);
        } else if !output.is_empty() {
            fs::write(&args.output, &output).context("Failed to write output")?;
        }
    }

    Ok(())
}

/// Encodes RGB pixel data as PPM P6 format (8-bit per channel).
///
/// # Arguments
/// * `width` - Image width in pixels
/// * `height` - Image height in pixels
/// * `rgb_data` - RGB pixel data (3 bytes per pixel, row-major order)
///
/// # Returns
/// A vector containing the complete PPM file (header + pixel data)
pub fn encode_ppm_rgb8(width: u32, height: u32, rgb_data: &[u8]) -> Result<Vec<u8>> {
    use std::io::Write;

    let expected_size = (width as usize) * (height as usize) * 3;
    if rgb_data.len() != expected_size {
        anyhow::bail!(
            "RGB data size mismatch: expected {} bytes, got {}",
            expected_size,
            rgb_data.len()
        );
    }

    let mut output = Vec::with_capacity(20 + rgb_data.len());
    write!(&mut output, "P6\n{} {}\n255\n", width, height)?;
    output.write_all(rgb_data)?;
    Ok(output)
}

/// Encodes RGB pixel data as PPM P6 format (16-bit per channel).
///
/// # Arguments
/// * `width` - Image width in pixels
/// * `height` - Image height in pixels
/// * `rgb_data` - RGB pixel data (u16 values, 3 values per pixel, row-major order)
///
/// # Returns
/// A vector containing the complete PPM file (header + pixel data in big-endian)
pub fn encode_ppm_rgb16(width: u32, height: u32, rgb_data: &[u16]) -> Result<Vec<u8>> {
    use std::io::Write;

    let expected_size = (width as usize) * (height as usize) * 3;
    if rgb_data.len() != expected_size {
        anyhow::bail!(
            "RGB data size mismatch: expected {} u16 values, got {}",
            expected_size,
            rgb_data.len()
        );
    }

    let mut output = Vec::with_capacity(20 + rgb_data.len() * 2);
    write!(&mut output, "P6\n{} {}\n65535\n", width, height)?;
    for val in rgb_data {
        output.write_all(&val.to_be_bytes())?;
    }
    Ok(output)
}

/// Decodes a PPM P6 file (8-bit per channel) to RGB pixel data.
///
/// # Arguments
/// * `path` - Path to the PPM file
///
/// # Returns
/// A tuple containing (width, height, rgb_data)
pub fn decode_ppm_rgb8(path: &std::path::Path) -> Result<(u32, u32, Vec<u8>)> {
    let input_data = fs::read(path).context("Failed to read input file")?;
    let img = image::load_from_memory_with_format(&input_data, image::ImageFormat::Pnm)
        .context("Failed to decode input PPM")?;
    let rgb = img.to_rgb8();
    Ok((rgb.width(), rgb.height(), rgb.to_vec()))
}
