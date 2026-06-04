use anyhow::{Context, Result};
use benchmark_harness::{Args, BenchmarkImplementation};
use image::codecs::png::{CompressionType, FilterType, PngEncoder};
use image::ImageEncoder;
use std::io::BufWriter;

struct ImagePngBench;

struct BenchContext {
    rgb_data: Vec<u8>,
    width: u32,
    height: u32,
    compression: CompressionType,
    filter: FilterType,
}

impl BenchmarkImplementation for ImagePngBench {
    fn name(&self) -> &'static str {
        "image-png-encode"
    }

    fn prepare(&self, args: &Args) -> Result<Box<dyn std::any::Any>> {
        let (width, height, rgb_data) = benchmark_harness::decode_ppm_rgb8(&args.input)?;

        // Map quality tier to compression
        let (compression, filter) = match args.param_str("quality-tier", "web-high").as_str() {
            "web-low" => (CompressionType::Fast, FilterType::Sub),
            "archival" => (CompressionType::Best, FilterType::Adaptive),
            _ => (CompressionType::Default, FilterType::Paeth),
        };

        Ok(Box::new(BenchContext {
            rgb_data,
            width,
            height,
            compression,
            filter,
        }))
    }

    fn run(&self, _args: &Args, context: &mut dyn std::any::Any) -> Result<Vec<u8>> {
        let ctx = context
            .downcast_ref::<BenchContext>()
            .expect("Invalid context");

        let mut output = Vec::with_capacity(ctx.rgb_data.len() / 2);
        {
            let writer = BufWriter::new(&mut output);
            let encoder = PngEncoder::new_with_quality(writer, ctx.compression, ctx.filter);
            encoder
                .write_image(
                    &ctx.rgb_data,
                    ctx.width,
                    ctx.height,
                    image::ColorType::Rgb8.into(),
                )
                .context("Failed to encode PNG")?;
        }

        Ok(output)
    }
}

fn main() -> Result<()> {
    benchmark_harness::main(ImagePngBench)
}
