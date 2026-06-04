use anyhow::{Context, Result};
use benchmark_harness::{Args, BenchmarkImplementation};
use zune_core::bit_depth::BitDepth;
use zune_core::colorspace::ColorSpace;
use zune_core::options::EncoderOptions;
use zune_jpegxl::JxlSimpleEncoder;

struct ZuneJxlBench;

struct BenchContext {
    input_data: Vec<u8>,
    width: usize,
    height: usize,
    quality: u8,
    effort: u8,
}

impl BenchmarkImplementation for ZuneJxlBench {
    fn name(&self) -> &'static str {
        "zune-jpegxl-encode"
    }

    fn prepare(&self, args: &Args) -> Result<Box<dyn std::any::Any>> {
        let (w, h, rgb) = benchmark_harness::decode_ppm_rgb8(&args.input)
            .context("Failed to decode input PPM")?;
        let width = w as usize;
        let height = h as usize;
        let input_data = rgb;

        // TODO: see if zune exposes distance parameter as something not quality
        // setting (so the JXL quality axis can be `distance` like libjxl).
        let quality = args.param_u32("quality", 90) as u8;
        let effort = args.param_u32("effort", 7) as u8;

        Ok(Box::new(BenchContext {
            input_data,
            width,
            height,
            quality,
            effort,
        }))
    }

    fn run(&self, _args: &Args, context: &mut dyn std::any::Any) -> Result<Vec<u8>> {
        let ctx = context
            .downcast_ref::<BenchContext>()
            .expect("Invalid context");

        let options = EncoderOptions::new(ctx.width, ctx.height, ColorSpace::RGB, BitDepth::Eight)
            .set_effort(ctx.effort)
            .set_quality(ctx.quality);
        let encoder = JxlSimpleEncoder::new(&ctx.input_data, options);

        // Create output buffer - estimate size needed
        let estimated_size = ctx.input_data.len() * 2;
        let mut output = vec![0u8; estimated_size];

        let bytes_written = encoder
            .encode(&mut output[..])
            .context("Failed to encode JXL")?;

        output.truncate(bytes_written);
        Ok(output)
    }
}

fn main() -> Result<()> {
    benchmark_harness::main(ZuneJxlBench)
}
