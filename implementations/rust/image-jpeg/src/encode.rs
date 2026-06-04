use anyhow::{Context, Result};
use benchmark_harness::{Args, BenchmarkImplementation};
use image::codecs::jpeg::JpegEncoder;
use image::ImageEncoder;

struct ImageJpegBench;

struct BenchContext {
    rgb_data: Vec<u8>,
    width: u32,
    height: u32,
    quality: u8,
}

impl BenchmarkImplementation for ImageJpegBench {
    fn name(&self) -> &'static str {
        "image-jpeg-encode"
    }

    fn prepare(&self, args: &Args) -> Result<Box<dyn std::any::Any>> {
        let (width, height, rgb_data) = benchmark_harness::decode_ppm_rgb8(&args.input)?;

        // Map quality tier to JPEG quality (1-100)
        let quality: u8 = match args.param_str("quality-tier", "web-high").as_str() {
            "web-low" => 50,
            "archival" => 95,
            _ => 80,
        };

        Ok(Box::new(BenchContext {
            rgb_data,
            width,
            height,
            quality,
        }))
    }

    fn run(&self, _args: &Args, context: &mut dyn std::any::Any) -> Result<Vec<u8>> {
        let ctx = context
            .downcast_ref::<BenchContext>()
            .expect("Invalid context");

        let mut output = Vec::with_capacity(ctx.rgb_data.len() / 2);
        {
            // LIMITATION: image::codecs::jpeg::JpegEncoder does not support configuring
            // progressive encoding or chroma subsampling (crate limitation as of image 0.25).
            // Quality tiers are differentiated only by the quality value.
            let encoder = JpegEncoder::new_with_quality(&mut output, ctx.quality);
            encoder
                .write_image(
                    &ctx.rgb_data,
                    ctx.width,
                    ctx.height,
                    image::ColorType::Rgb8.into(),
                )
                .context("Failed to encode JPEG")?;
        }

        Ok(output)
    }
}

fn main() -> Result<()> {
    benchmark_harness::main(ImageJpegBench)
}
