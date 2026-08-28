import CoreVideo
import Testing
@testable import VideoToNDICore

@Suite
struct VideoFrameTransformerTests {
    @Test func identityTransformKeepsOriginalDimensionsAndBuffer() throws {
        let input = try makeFrame(width: 4, height: 3)
        let transformer = VideoFrameTransformer(
            rotation: .degrees0,
            flipHorizontal: false,
            flipVertical: false
        )

        let output = try transformer.transform(input)

        #expect(output.width == 4)
        #expect(output.height == 3)
        #expect(CFEqual(output.pixelBuffer, input.pixelBuffer))
    }

    @Test func quarterTurnSwapsOutputDimensions() throws {
        let input = try makeFrame(width: 4, height: 3)

        for rotation in [VideoRotation.degrees90, .degrees270] {
            let transformer = VideoFrameTransformer(
                rotation: rotation,
                flipHorizontal: false,
                flipVertical: false
            )
            let output = try transformer.transform(input)

            #expect(output.width == 3)
            #expect(output.height == 4)
            #expect(CVPixelBufferGetPixelFormatType(output.pixelBuffer) == kCVPixelFormatType_32BGRA)
        }
    }

    @Test func flipsKeepOutputDimensions() throws {
        let input = try makeFrame(width: 4, height: 3)
        let transformer = VideoFrameTransformer(
            rotation: .degrees180,
            flipHorizontal: true,
            flipVertical: true
        )

        let output = try transformer.transform(input)

        #expect(output.width == 4)
        #expect(output.height == 3)
    }

    private func makeFrame(width: Int, height: Int) throws -> VideoFrame {
        var buffer: CVPixelBuffer?
        let attributes: [CFString: Any] = [
            kCVPixelBufferIOSurfacePropertiesKey: [:],
        ]
        let result = CVPixelBufferCreate(
            kCFAllocatorDefault,
            width,
            height,
            kCVPixelFormatType_32BGRA,
            attributes as CFDictionary,
            &buffer
        )
        #expect(result == kCVReturnSuccess)
        return VideoFrame(pixelBuffer: try #require(buffer), width: width, height: height)
    }
}
