import Foundation
import VideoToNDICore

let status = NDIRuntime.shared.initialize()
if status.available {
    print("NDI runtime available: \(status.version)")
    print(status.message)
    exit(EXIT_SUCCESS)
} else {
    fputs("NDI runtime unavailable: \(status.message)\n", stderr)
    exit(EXIT_FAILURE)
}
