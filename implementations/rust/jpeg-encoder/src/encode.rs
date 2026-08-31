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
        let quality = args.param_u32("quality", 80) as u8;
        let is_progressive = args.param_bool("progressive", true);
        let sampling_factor = if args.param_str("subsampling", "420") == "444" {
            SamplingFactor::R_4_4_4
        } else {
            SamplingFactor::R_4_2_0
        };
        // JPEG's own dimension field is 16 bits, so u16 is the right target
        // type -- but a bare `as u16` wraps silently past 65535 and would encode
        // at a wrong, quietly plausible size. Fail loudly instead.
        let width = u16::try_from(width)
            .with_context(|| format!("image width {width} exceeds JPEG's 65535 px limit"))?;
        let height = u16::try_from(height)
            .with_context(|| format!("image height {height} exceeds JPEG's 65535 px limit"))?;
        Ok(Box::new(BenchContext {
            quality,
            is_progressive,
            sampling_factor,
            width,
            height,
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

#[cfg(test)]
mod tests {
    /// JPEG stores dimensions in 16 bits, so anything above 65535 cannot be
    /// represented. Guard the conversion the encoder relies on: `as u16` wraps
    /// (65536 -> 0), `try_from` refuses, which is what `prepare` now does.
    #[test]
    fn dimensions_above_u16_are_rejected_not_wrapped() {
        assert_eq!(65536u32 as u16, 0, "the silent-wrap hazard this guards");
        assert_eq!(70000u32 as u16, 4464);

        assert!(u16::try_from(65536u32).is_err());
        assert!(u16::try_from(70000u32).is_err());

        // Everything JPEG can actually represent still converts cleanly.
        assert_eq!(u16::try_from(65535u32).unwrap(), 65535);
        assert_eq!(u16::try_from(4096u32).unwrap(), 4096);
        assert_eq!(u16::try_from(1u32).unwrap(), 1);
    }
}
