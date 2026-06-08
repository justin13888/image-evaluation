//! `iqa-cli` — a command-line front-end to the [`iqa`] crate.
//!
//! It decodes two images (a reference and a distorted version), computes one or
//! more full-reference quality metrics between them, and prints the scores as a
//! JSON object keyed by metric name. All metric math lives in the `iqa` crate;
//! this binary only decodes the inputs — the one job `iqa` deliberately leaves
//! to the caller — and serializes the results.
//!
//! ```text
//! iqa-cli --reference ref.png --distorted out.png --metric ssimulacra2,psnr,ssim,butteraugli
//! # -> {"butteraugli":0.83,"psnr":38.114,"ssim":0.992,"ssimulacra2":87.421}
//! ```
//!
//! Non-finite scores (e.g. the PSNR of two pixel-identical images is `+inf`) are
//! emitted as JSON `null`. With no `--metric`, every available metric is
//! computed; `--list-metrics` prints the full set and each metric's direction.
//!
//! [`iqa`]: https://crates.io/crates/iqa

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use clap::Parser;
use iqa::{ButteraugliOptions, Image, PsnrOptions, Srgb8, SsimOptions};
use serde_json::{Map, Value};

/// A full-reference image-quality metric this CLI can compute.
struct MetricDef {
    /// CLI name, accepted by `--metric` and emitted as the JSON key.
    name: &'static str,
    /// Whether a higher score means better fidelity. `false` for Butteraugli,
    /// where `0.0` is identical and larger is worse.
    higher_is_better: bool,
    /// Computes the metric between a reference and a distorted image.
    compute: fn(&Image<Srgb8>, &Image<Srgb8>) -> iqa::Result<f64>,
}

fn compute_ssimulacra2(reference: &Image<Srgb8>, distorted: &Image<Srgb8>) -> iqa::Result<f64> {
    iqa::ssimulacra2(reference, distorted)
}
fn compute_psnr(reference: &Image<Srgb8>, distorted: &Image<Srgb8>) -> iqa::Result<f64> {
    iqa::psnr(reference, distorted, PsnrOptions::default())
}
fn compute_ssim(reference: &Image<Srgb8>, distorted: &Image<Srgb8>) -> iqa::Result<f64> {
    iqa::ssim(reference, distorted, SsimOptions::default())
}
fn compute_butteraugli(reference: &Image<Srgb8>, distorted: &Image<Srgb8>) -> iqa::Result<f64> {
    iqa::butteraugli(reference, distorted, ButteraugliOptions::default())
}

/// Every metric this CLI can compute. Add a metric by adding a row here — the
/// `--metric` parser, the default set, `--list-metrics`, and the
/// unknown-metric error all derive from this table.
const METRICS: &[MetricDef] = &[
    MetricDef {
        name: "ssimulacra2",
        higher_is_better: true,
        compute: compute_ssimulacra2,
    },
    MetricDef {
        name: "psnr",
        higher_is_better: true,
        compute: compute_psnr,
    },
    MetricDef {
        name: "ssim",
        higher_is_better: true,
        compute: compute_ssim,
    },
    MetricDef {
        name: "butteraugli",
        higher_is_better: false,
        compute: compute_butteraugli,
    },
];

fn lookup(name: &str) -> Option<&'static MetricDef> {
    METRICS.iter().find(|m| m.name == name)
}

fn available_metrics() -> String {
    METRICS
        .iter()
        .map(|m| m.name)
        .collect::<Vec<_>>()
        .join(", ")
}

#[derive(Parser, Debug)]
#[command(author, version, about = "Image-quality metrics via the iqa crate", long_about = None)]
struct Args {
    /// Reference (original) image. PNG/JPEG/PPM.
    #[arg(long)]
    reference: Option<PathBuf>,

    /// Distorted image to compare against the reference. PNG/JPEG/PPM.
    #[arg(long)]
    distorted: Option<PathBuf>,

    /// Comma-separated metrics to compute. Defaults to every available metric;
    /// see `--list-metrics`.
    #[arg(long)]
    metric: Option<String>,

    /// Output format. Only `json` is currently supported.
    #[arg(long, default_value = "json")]
    format: String,

    /// List the available metrics and their direction, then exit.
    #[arg(long)]
    list_metrics: bool,
}

/// Decodes an image file into the RGB8 buffer `iqa` consumes.
fn load(path: &Path) -> Result<Image<Srgb8>> {
    let decoded = image::open(path)
        .with_context(|| format!("failed to decode image: {}", path.display()))?
        .to_rgb8();
    let (width, height) = decoded.dimensions();
    Image::srgb8(width, height, decoded.into_raw())
        .with_context(|| format!("failed to build iqa image from {}", path.display()))
}

/// Parses a `--metric` spec into an order-preserving list of names, or the full
/// set when none was given.
fn requested_metrics(spec: Option<&str>) -> Vec<&str> {
    match spec {
        Some(s) => s
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .collect(),
        None => METRICS.iter().map(|m| m.name).collect(),
    }
}

/// Computes the named metrics and returns them as a JSON object. Non-finite
/// scores serialize as `null` (`serde_json` maps non-finite floats to null).
fn compute(reference: &Image<Srgb8>, distorted: &Image<Srgb8>, metrics: &[&str]) -> Result<Value> {
    let mut out = Map::new();
    for &name in metrics {
        let def = lookup(name).with_context(|| {
            format!(
                "unknown metric '{name}' (available: {})",
                available_metrics()
            )
        })?;
        let score =
            (def.compute)(reference, distorted).with_context(|| format!("{name} failed"))?;
        out.insert(def.name.to_string(), Value::from(score));
    }
    Ok(Value::Object(out))
}

/// Loads the inputs, computes the requested metrics, and returns the JSON line.
fn run(args: &Args) -> Result<String> {
    if args.format != "json" {
        anyhow::bail!("unsupported --format '{}' (only 'json')", args.format);
    }
    let reference = args.reference.as_ref().context("--reference is required")?;
    let distorted = args.distorted.as_ref().context("--distorted is required")?;
    let reference = load(reference)?;
    let distorted = load(distorted)?;
    let metrics = requested_metrics(args.metric.as_deref());
    let value = compute(&reference, &distorted, &metrics)?;
    Ok(serde_json::to_string(&value)?)
}

fn print_metric_list() {
    for m in METRICS {
        let dir = if m.higher_is_better {
            "higher is better"
        } else {
            "lower is better"
        };
        println!("{:<14} {dir}", m.name);
    }
}

fn main() -> Result<()> {
    let args = Args::parse();
    if args.list_metrics {
        print_metric_list();
        return Ok(());
    }
    println!("{}", run(&args)?);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A deterministic gradient, large enough for SSIMULACRA2's and
    /// Butteraugli's multi-scale downsampling.
    fn sample() -> Image<Srgb8> {
        let (w, h) = (64u32, 64u32);
        let mut data = Vec::with_capacity((w * h * 3) as usize);
        for y in 0..h {
            for x in 0..w {
                data.push((x * 4 % 256) as u8);
                data.push((y * 4 % 256) as u8);
                data.push(((x + y) * 2 % 256) as u8);
            }
        }
        Image::srgb8(w, h, data).unwrap()
    }

    /// `sample()` brightened uniformly — a clear distortion in every metric.
    fn distorted() -> Image<Srgb8> {
        let base = sample();
        let bytes: Vec<u8> = base
            .samples()
            .iter()
            .map(|&b| b.saturating_add(40))
            .collect();
        Image::srgb8(64, 64, bytes).unwrap()
    }

    #[test]
    fn identical_images_score_perfectly() {
        let img = sample();
        assert!((compute_ssimulacra2(&img, &img).unwrap() - 100.0).abs() < 1e-4);
        assert!((compute_ssim(&img, &img).unwrap() - 1.0).abs() < 1e-6);
        assert!(compute_butteraugli(&img, &img).unwrap().abs() < 1e-3);
        assert!(!compute_psnr(&img, &img).unwrap().is_finite()); // +inf
    }

    #[test]
    fn non_finite_scores_serialize_as_null() {
        let img = sample();
        let v = compute(&img, &img, &["psnr"]).unwrap();
        assert!(v.get("psnr").unwrap().is_null());
    }

    #[test]
    fn distortion_moves_each_metric_in_its_direction() {
        let (reference, distorted) = (sample(), distorted());
        assert!(compute_ssimulacra2(&reference, &distorted).unwrap() < 100.0);
        assert!(compute_ssim(&reference, &distorted).unwrap() < 1.0);
        assert!(compute_butteraugli(&reference, &distorted).unwrap() > 0.0);
        assert!(compute_psnr(&reference, &distorted).unwrap().is_finite());
    }

    #[test]
    fn unknown_metric_errors() {
        let img = sample();
        assert!(compute(&img, &img, &["bogus"]).is_err());
    }

    #[test]
    fn default_metric_set_is_all() {
        assert_eq!(requested_metrics(None).len(), METRICS.len());
        assert_eq!(requested_metrics(Some("psnr, ssim")), vec!["psnr", "ssim"]);
    }
}
