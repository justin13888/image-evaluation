use anyhow::{Context, Result};
use benchmark_harness::{Args, BenchmarkImplementation};
use zenpixels_convert::PixelBufferConvertTypedExt as _;

struct ZenavifDecodeBench;

struct BenchContext {
    input_data: Vec<u8>,
}

impl BenchmarkImplementation for ZenavifDecodeBench {
    fn name(&self) -> &'static str {
        "zenavif-decode"
    }

    fn prepare(&self, args: &Args) -> Result<Box<dyn std::any::Any>> {
        let input_data = std::fs::read(&args.input).context("Failed to read input file")?;
        Ok(Box::new(BenchContext { input_data }))
    }

    fn run(&self, _args: &Args, context: &mut dyn std::any::Any) -> Result<Vec<u8>> {
        let ctx = context
            .downcast_ref::<BenchContext>()
            .expect("Invalid context");

        let image = zenavif::decode(&ctx.input_data)
            .map_err(|e| anyhow::anyhow!("zenavif decode failed: {e}"))?;
        // Decode yields 8/10/12-bit native pixels; normalise to interleaved RGB8.
        let rgb = image.to_rgb8();
        let w = rgb.width();
        let h = rgb.height();
        let bytes = rgb
            .as_contiguous_bytes()
            .context("zenavif decoded pixels were not contiguous")?;
        benchmark_harness::encode_ppm_rgb8(w, h, bytes)
    }
}

fn main() -> Result<()> {
    benchmark_harness::main(ZenavifDecodeBench)
}
