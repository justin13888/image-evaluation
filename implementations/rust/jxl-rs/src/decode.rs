use anyhow::{bail, Context, Result};
use benchmark_harness::{Args, BenchmarkImplementation};
use jxl::api::{
    states, JxlColorType, JxlDataFormat, JxlDecoder, JxlDecoderOptions, JxlOutputBuffer,
    JxlPixelFormat, ProcessingResult,
};
use jxl::image::{OwnedRawImage, Rect};
use std::fs;

struct JxlRsBench;

struct BenchContext {
    input_data: Vec<u8>,
}

impl BenchmarkImplementation for JxlRsBench {
    fn name(&self) -> &'static str {
        "jxl-rs-decode"
    }

    fn prepare(&self, args: &Args) -> Result<Box<dyn std::any::Any>> {
        let input_data = fs::read(&args.input).context("Failed to read input file")?;

        Ok(Box::new(BenchContext { input_data }))
    }

    fn run(&self, _args: &Args, context: &mut dyn std::any::Any) -> Result<Vec<u8>> {
        let ctx = context
            .downcast_ref::<BenchContext>()
            .expect("Invalid context");

        // The whole file is in memory, so the decoder never needs more input.
        let mut input: &[u8] = &ctx.input_data;

        // Stage 1: decode the image header to obtain basic info.
        let decoder = JxlDecoder::<states::Initialized>::new(JxlDecoderOptions::default());
        let mut decoder = match decoder.process(&mut input)? {
            ProcessingResult::Complete { result } => result,
            ProcessingResult::NeedsMoreInput { .. } => bail!("JXL input truncated (header)"),
        };

        let (width, height) = decoder.basic_info().size;

        // Request interleaved 8-bit RGB and ignore any extra channels (e.g. alpha),
        // mirroring jxl-oxide's `to_rgb8()` so decoders are compared on the same output.
        let extra_channel_format = decoder
            .current_pixel_format()
            .extra_channel_format
            .iter()
            .map(|_| None)
            .collect();
        decoder.set_pixel_format(JxlPixelFormat {
            color_type: JxlColorType::Rgb,
            color_data_format: Some(JxlDataFormat::U8 { bit_depth: 8 }),
            extra_channel_format,
        });

        let samples_per_pixel = JxlColorType::Rgb.samples_per_pixel();

        // Single interleaved color buffer: width * samples bytes per row, height rows.
        let mut color = OwnedRawImage::new((width * samples_per_pixel, height))?;

        // Stage 2: read the frame header.
        let frame_decoder = match decoder.process(&mut input)? {
            ProcessingResult::Complete { result } => result,
            ProcessingResult::NeedsMoreInput { .. } => bail!("JXL input truncated (frame header)"),
        };

        // Stage 3: decode the frame's pixels into the color buffer.
        {
            let rect = Rect {
                size: color.byte_size(),
                origin: (0, 0),
            };
            let mut buffers = vec![JxlOutputBuffer::from_image_rect_mut(
                color.get_rect_mut(rect),
            )];
            match frame_decoder.process(&mut input, &mut buffers)? {
                ProcessingResult::Complete { .. } => {}
                ProcessingResult::NeedsMoreInput { .. } => {
                    bail!("JXL input truncated (frame data)")
                }
            }
        }

        // Concatenate the interleaved RGB rows into a contiguous buffer for PPM output.
        let mut rgb = Vec::with_capacity(width * height * samples_per_pixel);
        for y in 0..height {
            rgb.extend_from_slice(color.row(y));
        }

        benchmark_harness::encode_ppm_rgb8(width as u32, height as u32, &rgb)
    }
}

fn main() -> Result<()> {
    benchmark_harness::main(JxlRsBench)
}
