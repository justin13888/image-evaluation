use almost_enough::{StopExt as _, Unstoppable};
use anyhow::Result;
use benchmark_harness::{Args, BenchmarkImplementation};
use imgref::ImgRef;
use rgb::Rgb;
use zenavif::{encode_rgb8, EncoderConfig};

struct ZenavifEncodeBench;

struct BenchContext {
    rgb: Vec<u8>,
    width: usize,
    height: usize,
    quality: f32,
    speed: u8,
}

impl BenchmarkImplementation for ZenavifEncodeBench {
    fn name(&self) -> &'static str {
        "zenavif-encode"
    }

    fn prepare(&self, args: &Args) -> Result<Box<dyn std::any::Any>> {
        let (width, height, rgb) = benchmark_harness::decode_ppm_rgb8(&args.input)?;
        let quality = args.param_f32("quality", 65.0).clamp(1.0, 100.0);
        let speed = args.param_u32("speed", 6).clamp(1, 10) as u8;
        Ok(Box::new(BenchContext {
            rgb,
            width: width as usize,
            height: height as usize,
            quality,
            speed,
        }))
    }

    fn run(&self, _args: &Args, context: &mut dyn std::any::Any) -> Result<Vec<u8>> {
        let ctx = context
            .downcast_ref::<BenchContext>()
            .expect("Invalid context");

        let pixels: &[Rgb<u8>] = bytemuck::cast_slice(&ctx.rgb);
        let img = ImgRef::new(pixels, ctx.width, ctx.height);
        // zenavif 0.1.x exposes quality + speed (chroma subsampling is not yet a
        // public knob; the encoder picks its default).
        let config = EncoderConfig::new().quality(ctx.quality).speed(ctx.speed);
        let encoded = encode_rgb8(img, &config, Unstoppable.into_token())
            .map_err(|e| anyhow::anyhow!("zenavif encode failed: {e}"))?;
        Ok(encoded.avif_file)
    }
}

fn main() -> Result<()> {
    benchmark_harness::main(ZenavifEncodeBench)
}
