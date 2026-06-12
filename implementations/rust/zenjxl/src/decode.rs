use anyhow::{Context, Result};
use benchmark_harness::{Args, BenchmarkImplementation};
use zenpixels_convert::PixelBufferConvertTypedExt as _;

struct ZenjxlDecodeBench;

struct BenchContext {
    input_data: Vec<u8>,
}

impl BenchmarkImplementation for ZenjxlDecodeBench {
    fn name(&self) -> &'static str {
        "zenjxl-decode"
    }

    fn prepare(&self, args: &Args) -> Result<Box<dyn std::any::Any>> {
        let input_data = std::fs::read(&args.input).context("Failed to read input file")?;
        Ok(Box::new(BenchContext { input_data }))
    }

    fn run(&self, _args: &Args, context: &mut dyn std::any::Any) -> Result<Vec<u8>> {
        let ctx = context
            .downcast_ref::<BenchContext>()
            .expect("Invalid context");

        // No resource limits; let the decoder pick its native pixel format.
        let output = zenjxl::decode(&ctx.input_data, None, &[])
            .map_err(|e| anyhow::anyhow!("zenjxl decode failed: {e}"))?;
        let (w, h) = (output.info.width, output.info.height);
        let rgb = output.pixels.to_rgb8();
        let bytes = rgb
            .as_contiguous_bytes()
            .context("zenjxl decoded pixels were not contiguous")?;
        benchmark_harness::encode_ppm_rgb8(w, h, bytes)
    }
}

fn main() -> Result<()> {
    benchmark_harness::main(ZenjxlDecodeBench)
}
