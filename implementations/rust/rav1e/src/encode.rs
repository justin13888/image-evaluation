use anyhow::{Context, Result};
use benchmark_harness::{Args, BenchmarkImplementation};
use rav1e::prelude::*;

struct Rav1eBench;

struct BenchContext {
    width: usize,
    height: usize,
    rgb_data: Vec<u8>,
    quantizer: usize,
    speed: u8,
    chroma_sampling: ChromaSampling,
}

impl BenchmarkImplementation for Rav1eBench {
    fn name(&self) -> &'static str {
        "rav1e-encode"
    }

    fn prepare(&self, args: &Args) -> Result<Box<dyn std::any::Any>> {
        // Load the raw image data (PPM format expected)
        let (width, height, rgb_data) = benchmark_harness::decode_ppm_rgb8(&args.input)?;

        // rav1e quantizer (0=lossless, 255=worst) to AVIF quality (0-100) conversion:
        // rav1e_quantizer ≈ (100 - avif_quality) * 255 / 100
        // README target: web-low=Q65/Speed6, web-high=Q65/Grain Synth, archival=Q85/YUV444
        // Q65 → (100-65)*255/100 ≈ 89; Q85 → (100-85)*255/100 ≈ 38
        // TODO: web-high should enable film grain synthesis (rav1e EncoderConfig::film_grain_params)
        // to match README spec; currently omitted as it requires calibrated noise parameters.
        let (quantizer, speed, chroma_sampling) =
            match args.param_str("quality-tier", "web-high").as_str() {
                "web-low" => (89, 6u8, ChromaSampling::Cs420),
                "archival" => (38, 4u8, ChromaSampling::Cs444),
                _ => (89, 6u8, ChromaSampling::Cs420),
            };

        Ok(Box::new(BenchContext {
            width: width as usize,
            height: height as usize,
            rgb_data,
            quantizer,
            speed,
            chroma_sampling,
        }))
    }

    fn run(&self, _args: &Args, context: &mut dyn std::any::Any) -> Result<Vec<u8>> {
        let ctx = context
            .downcast_ref::<BenchContext>()
            .expect("Invalid context");

        // Configure encoder
        let mut enc_config = EncoderConfig {
            width: ctx.width,
            height: ctx.height,
            time_base: Rational::new(1, 30),
            bit_depth: 8,
            chroma_sampling: ctx.chroma_sampling,
            ..Default::default()
        };

        enc_config.quantizer = ctx.quantizer;
        enc_config.speed_settings = SpeedSettings::from_preset(ctx.speed);

        let cfg = Config::new().with_encoder_config(enc_config);

        let mut enc_ctx = cfg
            .new_context::<u8>()
            .context("Failed to create encoder context")?;

        // Create frame and convert RGB to YUV
        let mut frame = enc_ctx.new_frame();

        // Cache strides to avoid borrow conflicts
        let y_stride = frame.planes[0].cfg.stride;

        // Fill Y plane (BT.709 luma)
        for y in 0..ctx.height {
            for x in 0..ctx.width {
                let idx = (y * ctx.width + x) * 3;
                let r = ctx.rgb_data[idx] as f32;
                let g = ctx.rgb_data[idx + 1] as f32;
                let b = ctx.rgb_data[idx + 2] as f32;
                let y_val = (0.2126 * r + 0.7152 * g + 0.0722 * b) as u8;
                frame.planes[0].data_origin_mut()[y * y_stride + x] = y_val;
            }
        }

        let u_stride = frame.planes[1].cfg.stride;
        let v_stride = frame.planes[2].cfg.stride;

        match ctx.chroma_sampling {
            ChromaSampling::Cs420 => {
                // Average 2x2 blocks for chroma downsampling
                for y in (0..ctx.height).step_by(2) {
                    for x in (0..ctx.width).step_by(2) {
                        let mut r_sum = 0.0f32;
                        let mut g_sum = 0.0f32;
                        let mut b_sum = 0.0f32;
                        let mut count = 0u32;
                        for dy in 0..2usize {
                            for dx in 0..2usize {
                                let py = (y + dy).min(ctx.height - 1);
                                let px = (x + dx).min(ctx.width - 1);
                                let idx = (py * ctx.width + px) * 3;
                                r_sum += ctx.rgb_data[idx] as f32;
                                g_sum += ctx.rgb_data[idx + 1] as f32;
                                b_sum += ctx.rgb_data[idx + 2] as f32;
                                count += 1;
                            }
                        }
                        let r = r_sum / count as f32;
                        let g = g_sum / count as f32;
                        let b = b_sum / count as f32;
                        let y_val = 0.2126 * r + 0.7152 * g + 0.0722 * b;
                        let u_val = ((b - y_val) * 0.539 + 128.0).clamp(0.0, 255.0) as u8;
                        let v_val = ((r - y_val) * 0.635 + 128.0).clamp(0.0, 255.0) as u8;
                        let uv_y = y / 2;
                        let uv_x = x / 2;
                        frame.planes[1].data_origin_mut()[uv_y * u_stride + uv_x] = u_val;
                        frame.planes[2].data_origin_mut()[uv_y * v_stride + uv_x] = v_val;
                    }
                }
            }
            ChromaSampling::Cs444 => {
                // Full chroma resolution — one U/V per pixel
                for y in 0..ctx.height {
                    for x in 0..ctx.width {
                        let idx = (y * ctx.width + x) * 3;
                        let r = ctx.rgb_data[idx] as f32;
                        let g = ctx.rgb_data[idx + 1] as f32;
                        let b = ctx.rgb_data[idx + 2] as f32;
                        let y_val = 0.2126 * r + 0.7152 * g + 0.0722 * b;
                        let u_val = ((b - y_val) * 0.539 + 128.0).clamp(0.0, 255.0) as u8;
                        let v_val = ((r - y_val) * 0.635 + 128.0).clamp(0.0, 255.0) as u8;
                        frame.planes[1].data_origin_mut()[y * u_stride + x] = u_val;
                        frame.planes[2].data_origin_mut()[y * v_stride + x] = v_val;
                    }
                }
            }
            _ => unreachable!("Unsupported chroma sampling"),
        }

        enc_ctx.send_frame(frame).context("Failed to send frame")?;
        enc_ctx.flush();

        let mut encoded_data = Vec::new();
        loop {
            match enc_ctx.receive_packet() {
                Ok(pkt) => {
                    encoded_data.extend_from_slice(&pkt.data);
                }
                Err(EncoderStatus::Encoded) | Err(EncoderStatus::LimitReached) => break,
                Err(e) => anyhow::bail!("Encoding error: {e:?}"),
            }
        }

        // TODO: Verify avif_serialize produces valid AVIF files.
        // The raw AV1 bitstream from rav1e needs proper ISOBMFF container wrapping.
        // Test output files with `avifenc --info` or similar tools.
        let output = avif_serialize::serialize_to_vec(
            &encoded_data,
            None,
            ctx.width as u32,
            ctx.height as u32,
            8,
        );

        Ok(output)
    }
}

fn main() -> Result<()> {
    benchmark_harness::main(Rav1eBench)
}
