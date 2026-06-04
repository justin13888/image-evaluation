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

        // Compression effort + row filter (PNG is lossless; no quality axis).
        let compression = match args.param_str("compression", "default").as_str() {
            "fast" => CompressionType::Fast,
            "best" => CompressionType::Best,
            _ => CompressionType::Default,
        };
        let filter = match args.param_str("filter", "adaptive").as_str() {
            "none" => FilterType::NoFilter,
            "sub" => FilterType::Sub,
            "up" => FilterType::Up,
            "avg" => FilterType::Avg,
            "paeth" => FilterType::Paeth,
            _ => FilterType::Adaptive,
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
