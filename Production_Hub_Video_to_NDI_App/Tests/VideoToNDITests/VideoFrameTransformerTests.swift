import CoreVideo
import XCTest
@testable import VideoToNDICore

final class VideoFrameTransformerTests: XCTestCase {
    func testIdentityTransformKeepsOriginalDimensionsAndBuffer() throws {
        let input = try makeFrame(width: 4, height: 3)
        let transformer = VideoFrameTransformer(
            rotation: .degrees0,
            flipHorizontal: false,
            flipVertical: false
        )

        let output = try transformer.transform(input)

        XCTAssertEqual(output.width, 4)
        XCTAssertEqual(output.height, 3)
        XCTAssertTrue(CFEqual(output.pixelBuffer, input.pixelBuffer))
    }

    func testQuarterTurnSwapsOutputDimensions() throws {
        let input = try makeFrame(width: 4, height: 3)

        for rotation in [VideoRotation.degrees90, .degrees270] {
            let transformer = VideoFrameTransformer(
                rotation: rotation,
                flipHorizontal: false,
                flipVertical: false
            )
            let output = try transformer.transform(input)

            XCTAssertEqual(output.width, 3)
            XCTAssertEqual(output.height, 4)
            XCTAssertEqual(CVPixelBufferGetPixelFormatType(output.pixelBuffer), kCVPixelFormatType_32BGRA)
        }
    }

    func testFlipsKeepOutputDimensions() throws {
        let input = try makeFrame(width: 4, height: 3)
        let transformer = VideoFrameTransformer(
            rotation: .degrees180,
            flipHorizontal: true,
            flipVertical: true
        )

        let output = try transformer.transform(input)

        XCTAssertEqual(output.width, 4)
        XCTAssertEqual(output.height, 3)
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
        XCTAssertEqual(result, kCVReturnSuccess)
        return VideoFrame(pixelBuffer: try XCTUnwrap(buffer), width: width, height: height)
    }
}

