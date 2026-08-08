import CoreImage
import CoreVideo
import Foundation
import ImageIO

enum VideoTransformError: LocalizedError {
    case cannotCreatePixelBufferPool(Int, Int)
    case cannotCreatePixelBuffer(Int, Int)

    var errorDescription: String? {
        switch self {
        case .cannotCreatePixelBufferPool(let width, let height):
            return "Could not prepare the \(width)×\(height) orientation buffer."
        case .cannotCreatePixelBuffer(let width, let height):
            return "Could not allocate a \(width)×\(height) oriented video frame."
        }
    }
}

final class VideoFrameTransformer {
    private let rotation: VideoRotation
    private let flipHorizontal: Bool
    private let flipVertical: Bool
    private let context = CIContext(options: [.cacheIntermediates: false])
    private let colorSpace = CGColorSpaceCreateDeviceRGB()
    private var pixelBufferPool: CVPixelBufferPool?
    private var poolWidth = 0
    private var poolHeight = 0

    init(rotation: VideoRotation, flipHorizontal: Bool, flipVertical: Bool) {
        self.rotation = rotation
        self.flipHorizontal = flipHorizontal
        self.flipVertical = flipVertical
    }

    convenience init(route: VideoRoute) {
        self.init(
            rotation: route.rotation,
            flipHorizontal: route.flipHorizontal,
            flipVertical: route.flipVertical
        )
    }

    func transform(_ frame: VideoFrame) throws -> VideoFrame {
        guard rotation != .degrees0 || flipHorizontal || flipVertical else { return frame }

        var image = CIImage(cvPixelBuffer: frame.pixelBuffer)
        switch rotation {
        case .degrees0:
            break
        case .degrees90:
            image = image.oriented(.right)
        case .degrees180:
            image = image.oriented(.down)
        case .degrees270:
            image = image.oriented(.left)
        }

        if flipHorizontal {
            let extent = image.extent
            image = image.transformed(by: CGAffineTransform(
                a: -1,
                b: 0,
                c: 0,
                d: 1,
                tx: extent.minX + extent.maxX,
                ty: 0
            ))
        }
        if flipVertical {
            let extent = image.extent
            image = image.transformed(by: CGAffineTransform(
                a: 1,
                b: 0,
                c: 0,
                d: -1,
                tx: 0,
                ty: extent.minY + extent.maxY
            ))
        }

        let transformedExtent = image.extent
        let width = max(1, Int(transformedExtent.width.rounded()))
        let height = max(1, Int(transformedExtent.height.rounded()))
        let outputBounds = CGRect(x: 0, y: 0, width: width, height: height)
        let normalized = image
            .transformed(by: CGAffineTransform(
                translationX: -transformedExtent.minX,
                y: -transformedExtent.minY
            ))
            .cropped(to: outputBounds)

        let outputBuffer = try makePixelBuffer(width: width, height: height)
        context.render(normalized, to: outputBuffer, bounds: outputBounds, colorSpace: colorSpace)

        return VideoFrame(
            pixelBuffer: outputBuffer,
            width: width,
            height: height,
            frameRateNumerator: frame.frameRateNumerator,
            frameRateDenominator: frame.frameRateDenominator,
            timecode: frame.timecode,
            capturedAt: frame.capturedAt
        )
    }

    private func makePixelBuffer(width: Int, height: Int) throws -> CVPixelBuffer {
        if pixelBufferPool == nil || width != poolWidth || height != poolHeight {
            let poolAttributes: [CFString: Any] = [
                kCVPixelBufferPoolMinimumBufferCountKey: 3,
            ]
            let pixelAttributes: [CFString: Any] = [
                kCVPixelBufferPixelFormatTypeKey: Int(kCVPixelFormatType_32BGRA),
                kCVPixelBufferWidthKey: width,
                kCVPixelBufferHeightKey: height,
                kCVPixelBufferIOSurfacePropertiesKey: [:],
                kCVPixelBufferMetalCompatibilityKey: true,
            ]
            var createdPool: CVPixelBufferPool?
            let result = CVPixelBufferPoolCreate(
                kCFAllocatorDefault,
                poolAttributes as CFDictionary,
                pixelAttributes as CFDictionary,
                &createdPool
            )
            guard result == kCVReturnSuccess, let createdPool else {
                throw VideoTransformError.cannotCreatePixelBufferPool(width, height)
            }
            pixelBufferPool = createdPool
            poolWidth = width
            poolHeight = height
        }

        var outputBuffer: CVPixelBuffer?
        let result = CVPixelBufferPoolCreatePixelBuffer(
            kCFAllocatorDefault,
            pixelBufferPool!,
            &outputBuffer
        )
        guard result == kCVReturnSuccess, let outputBuffer else {
            throw VideoTransformError.cannotCreatePixelBuffer(width, height)
        }
        return outputBuffer
    }
}

