use anyhow::Result;
use benchmark_harness::{Args, BenchmarkImplementation};

struct OxipngEncodeBench;

struct BenchContext {
    rgb: Vec<u8>,
    width: u32,
    height: u32,
    level: Option<u8>, // None => max_compression (-o max)
    zopfli: bool,
    interlace: bool,
}

impl BenchmarkImplementation for OxipngEncodeBench {
    fn name(&self) -> &'static str {
        "oxipng-encode"
    }

    fn prepare(&self, args: &Args) -> Result<Box<dyn std::any::Any>> {
        let (width, height, rgb) = benchmark_harness::decode_ppm_rgb8(&args.input)?;
        // oxipng's quality-relevant axis is its optimization level: 0..=6 -> from_preset,
        // "max" -> max_compression(). Default -o 2 matches oxipng's own default.
        let level = match args.param_str("level", "2").as_str() {
            "max" => None,
            other => Some(other.parse::<u8>().unwrap_or(2).min(6)),
        };
        // DEFLATE backend: libdeflate (fast, default) vs Zopfli (much slower, smaller).
        let zopfli = args.param_str("deflate", "libdeflate") == "zopfli";
        let interlace = args.param_bool("interlace", false);
        Ok(Box::new(BenchContext {
            rgb,
            width,
            height,
            level,
            zopfli,
            interlace,
        }))
    }

    fn run(&self, _args: &Args, context: &mut dyn std::any::Any) -> Result<Vec<u8>> {
        let ctx = context
            .downcast_ref::<BenchContext>()
            .expect("Invalid context");

        let mut opts = match ctx.level {
            Some(n) => oxipng::Options::from_preset(n),
            None => oxipng::Options::max_compression(),
        };
        if ctx.zopfli {
            opts.deflater = oxipng::Deflater::Zopfli(Default::default());
        }
        opts.interlace = Some(ctx.interlace);

        // RawImage encodes an optimized PNG directly from raw pixels (no baseline PNG
        // needed); it consumes the Vec, so clone the prepared pixels each iteration -- a
        // pixel memcpy that is negligible next to oxipng's filter/deflate trials.
        let raw = oxipng::RawImage::new(
            ctx.width,
            ctx.height,
            oxipng::ColorType::RGB {
                transparent_color: None,
            },
            oxipng::BitDepth::Eight,
            ctx.rgb.clone(),
        )
        .map_err(|e| anyhow::anyhow!("oxipng RawImage failed: {e}"))?;

        raw.create_optimized_png(&opts)
            .map_err(|e| anyhow::anyhow!("oxipng optimize failed: {e}"))
    }
}

fn main() -> Result<()> {
    benchmark_harness::main(OxipngEncodeBench)
}
