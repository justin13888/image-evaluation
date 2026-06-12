use anyhow::{Context, Result};
use benchmark_harness::{Args, BenchmarkImplementation};
use enough::Unstoppable;
use zenpixels_convert::PixelBufferConvertTypedExt as _;
use zenpng::{decode, PngDecodeConfig};

struct ZenpngDecodeBench;

struct BenchContext {
    input_data: Vec<u8>,
}

impl BenchmarkImplementation for ZenpngDecodeBench {
    fn name(&self) -> &'static str {
        "zenpng-decode"
    }

    fn prepare(&self, args: &Args) -> Result<Box<dyn std::any::Any>> {
        let input_data = std::fs::read(&args.input).context("Failed to read input file")?;
        Ok(Box::new(BenchContext { input_data }))
    }

    fn run(&self, _args: &Args, context: &mut dyn std::any::Any) -> Result<Vec<u8>> {
        let ctx = context
            .downcast_ref::<BenchContext>()
            .expect("Invalid context");

        let output = decode(&ctx.input_data, &PngDecodeConfig::default(), &Unstoppable)
            .map_err(|e| anyhow::anyhow!("zenpng decode failed: {e}"))?;
        // Decode yields the file's native color type; normalise to interleaved RGB8.
        let (w, h) = (output.info.width, output.info.height);
        let rgb = output.pixels.to_rgb8();
        let bytes = rgb
            .as_contiguous_bytes()
            .context("zenpng decoded pixels were not contiguous")?;
        benchmark_harness::encode_ppm_rgb8(w, h, bytes)
    }
}

fn main() -> Result<()> {
    benchmark_harness::main(ZenpngDecodeBench)
}
