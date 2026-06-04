use anyhow::{Context, Result};
use benchmark_harness::{Args, BenchmarkImplementation};
use jpeg_encoder::{ColorType, Encoder, SamplingFactor};

struct JpegEncoderBench;

struct BenchContext {
    quality: u8,
    is_progressive: bool,
    sampling_factor: SamplingFactor,
    width: u16,
    height: u16,
    rgb8_img: Vec<u8>,
}

impl BenchmarkImplementation for JpegEncoderBench {
    fn name(&self) -> &'static str {
        "jpeg-encoder-encode"
    }

    fn prepare(&self, args: &Args) -> Result<Box<dyn std::any::Any>> {
        let (width, height, rgb8_img) = benchmark_harness::decode_ppm_rgb8(&args.input)?;
        let (quality, is_progressive, sampling_factor) =
            match args.param_str("quality-tier", "web-high").as_str() {
                "web-low" => (50u8, false, SamplingFactor::R_4_2_0),
                "archival" => (95u8, false, SamplingFactor::R_4_4_4),
                _ => (80u8, true, SamplingFactor::R_4_2_0),
            };
        Ok(Box::new(BenchContext {
            quality,
            is_progressive,
            sampling_factor,
            width: width as u16,
            height: height as u16,
            rgb8_img,
        }))
    }

    fn run(&self, _args: &Args, context: &mut dyn std::any::Any) -> Result<Vec<u8>> {
        let ctx = context
            .downcast_ref::<BenchContext>()
            .expect("Invalid context");

        let mut output = Vec::with_capacity(ctx.rgb8_img.len());
        let mut encoder = Encoder::new(&mut output, ctx.quality);
        encoder.set_progressive(ctx.is_progressive);
        encoder.set_sampling_factor(ctx.sampling_factor);
        encoder
            .encode(&ctx.rgb8_img, ctx.width, ctx.height, ColorType::Rgb)
            .context("Failed to encode image")?;

        Ok(output)
    }
}

fn main() -> Result<()> {
    benchmark_harness::main(JpegEncoderBench)
}
