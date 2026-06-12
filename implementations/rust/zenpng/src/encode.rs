use anyhow::Result;
use benchmark_harness::{Args, BenchmarkImplementation};
use enough::Unstoppable;
use imgref::ImgRef;
use rgb::Rgb;
use zenpng::{Compression, EncodeConfig};

struct ZenpngEncodeBench;

struct BenchContext {
    rgb: Vec<u8>,
    width: u32,
    height: u32,
    effort: u32,
}

impl BenchmarkImplementation for ZenpngEncodeBench {
    fn name(&self) -> &'static str {
        "zenpng-encode"
    }

    fn prepare(&self, args: &Args) -> Result<Box<dyn std::any::Any>> {
        let (width, height, rgb) = benchmark_harness::decode_ppm_rgb8(&args.input)?;
        // zenpng's only quality-relevant knob is compression effort (0-200; lossless).
        // Filter selection is automatic. Default 13 = the `Balanced` preset.
        let effort = args.param_u32("effort", 13);
        Ok(Box::new(BenchContext {
            rgb,
            width,
            height,
            effort,
        }))
    }

    fn run(&self, _args: &Args, context: &mut dyn std::any::Any) -> Result<Vec<u8>> {
        let ctx = context
            .downcast_ref::<BenchContext>()
            .expect("Invalid context");

        let pixels: &[Rgb<u8>] = bytemuck::cast_slice(&ctx.rgb);
        let img = ImgRef::new(pixels, ctx.width as usize, ctx.height as usize);
        let config = EncodeConfig::default().with_compression(Compression::Effort(ctx.effort));
        zenpng::encode_rgb8(img, None, &config, &Unstoppable, &Unstoppable)
            .map_err(|e| anyhow::anyhow!("zenpng encode failed: {e}"))
    }
}

fn main() -> Result<()> {
    benchmark_harness::main(ZenpngEncodeBench)
}
