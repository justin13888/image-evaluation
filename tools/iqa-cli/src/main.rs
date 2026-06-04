//! `iqa-cli` — compute image-quality metrics for the benchmark's quality pass.
//!
//! The metric math comes entirely from the `iqa-rs` crate; this binary only
//! decodes the two inputs into raw RGB8 buffers (iqa-rs does not decode image
//! formats) and serializes the scores as JSON. The orchestrator decodes each
//! encoded output to PPM first, so both inputs are typically PPM, but any
//! PNG/JPEG the `image` crate understands also works.
//!
//! ```text
//! iqa-cli --reference ref.ppm --distorted out.ppm --metric ssimulacra2,psnr
//! # -> {"ssimulacra2": 87.421, "psnr": 38.114}
//! ```

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use clap::Parser;
use iqa_rs::{Image, PsnrOptions, Srgb8};

#[derive(Parser, Debug)]
#[command(author, version, about = "Image-quality metrics via iqa-rs", long_about = None)]
struct Args {
    /// Reference (original) image. PNG/JPEG/PPM.
    #[arg(long)]
    reference: PathBuf,

    /// Distorted image to compare against the reference. PNG/JPEG/PPM.
    #[arg(long)]
    distorted: PathBuf,

    /// Comma-separated metrics to compute. Supported now: `ssimulacra2`, `psnr`.
    /// (`butteraugli`, `ssim` are planned in iqa-rs — see TODO below.)
    #[arg(long, default_value = "ssimulacra2,psnr")]
    metric: String,

    /// Output format. Only `json` is currently supported.
    #[arg(long, default_value = "json")]
    format: String,
}

/// Decodes an image file into the RGB8 buffer iqa-rs consumes.
fn load(path: &Path) -> Result<Image<Srgb8>> {
    let decoded = image::open(path)
        .with_context(|| format!("failed to decode image: {}", path.display()))?
        .to_rgb8();
    let (width, height) = decoded.dimensions();
    Image::srgb8(width, height, decoded.into_raw())
        .with_context(|| format!("failed to build iqa-rs image from {}", path.display()))
}

/// Serializes an `f64` score as a JSON token. Non-finite scores (e.g. PSNR of
/// pixel-identical images is `+inf`) become `null` so the output stays valid
/// JSON; the orchestrator treats `null` as "no finite score".
fn json_f64(value: f64) -> String {
    if value.is_finite() {
        format!("{value}")
    } else {
        "null".to_string()
    }
}

fn main() -> Result<()> {
    let args = Args::parse();

    if args.format != "json" {
        anyhow::bail!("unsupported --format '{}' (only 'json')", args.format);
    }

    let reference = load(&args.reference)?;
    let distorted = load(&args.distorted)?;

    let mut fields: Vec<String> = Vec::new();
    for metric in args
        .metric
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        match metric {
            "psnr" => {
                let score = iqa_rs::psnr(&reference, &distorted, PsnrOptions::default())
                    .context("psnr failed")?;
                fields.push(format!("\"psnr\": {}", json_f64(score)));
            }
            "ssimulacra2" => {
                let score =
                    iqa_rs::ssimulacra2(&reference, &distorted).context("ssimulacra2 failed")?;
                fields.push(format!("\"ssimulacra2\": {}", json_f64(score)));
            }
            // TODO: wire `butteraugli` once iqa-rs exposes it (libjxl backend).
            // TODO: wire `ssim` once iqa-rs exposes it (native implementation).
            "butteraugli" | "ssim" => {
                anyhow::bail!("metric '{metric}' is not yet wired in iqa-cli (planned in iqa-rs)");
            }
            other => anyhow::bail!("unknown metric '{other}'"),
        }
    }

    println!("{{{}}}", fields.join(", "));
    Ok(())
}
