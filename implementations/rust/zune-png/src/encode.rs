use anyhow::{Context, Result};
use benchmark_harness::{Args, BenchmarkImplementation};
use zune_png::PngEncoder;

struct ZunePngBench;

struct BenchContext {
    input_data: Vec<u8>,
    width: usize,
    height: usize,
    effort: u8,
}

impl BenchmarkImplementation for ZunePngBench {
    fn name(&self) -> &'static str {
        "zune-png-encode"
    }

    fn prepare(&self, args: &Args) -> Result<Box<dyn std::any::Any>> {
        let (w, h, rgb) = benchmark_harness::decode_ppm_rgb8(&args.input)
            .context("Failed to decode input PPM")?;
        let width = w as usize;
        let height = h as usize;
        let input_data = rgb;

        // Map quality tier to zlib compression effort (0-9)
        let effort: u8 = match args.param_str("quality-tier", "web-high").as_str() {
            "web-low" => 1,
            "archival" => 9,
            _ => 5,
        };

        Ok(Box::new(BenchContext {
            input_data,
            width,
            height,
            effort,
        }))
    }

    fn run(&self, _args: &Args, context: &mut dyn std::any::Any) -> Result<Vec<u8>> {
        let ctx = context
            .downcast_ref::<BenchContext>()
            .expect("Invalid context");

        let options = zune_core::options::EncoderOptions::new(
            ctx.width,
            ctx.height,
            zune_core::colorspace::ColorSpace::RGB,
            zune_core::bit_depth::BitDepth::Eight,
        )
        .set_effort(ctx.effort);

        let mut encoder = PngEncoder::new(&ctx.input_data, options);

        // Pre-allocate output buffer
        let estimated_size = ctx.input_data.len();
        let mut output = Vec::with_capacity(estimated_size + 1024);

        let bytes_written = encoder
            .encode(&mut output)
            .map_err(|e| anyhow::anyhow!("Failed to encode PNG: {e:?}"))?;
        output.truncate(bytes_written);

        Ok(output)
    }
}

fn main() -> Result<()> {
    benchmark_harness::main(ZunePngBench)
}
