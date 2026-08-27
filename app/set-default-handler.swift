// Makes an app bundle the default application for Markdown files.
//
// There is no supported command-line tool for this on a stock macOS install
// (`duti` is the usual answer, but it is a Homebrew package), so this calls the
// system API directly.
//
// Two APIs exist, and the choice matters:
//
//   * LSSetDefaultRoleHandlerForContentType is the classic one. On current
//     macOS it still returns noErr but silently fails to change the
//     association - it reports success and does nothing.
//   * NSWorkspace.setDefaultApplication(at:toOpenContentType:), macOS 14+, is
//     the supported replacement and actually works.
//
// So: the modern call where available, the legacy one as a fallback for older
// systems.
//
// Usage: swift set-default-handler.swift /path/to/mdlive.app

import AppKit
import CoreServices
import Foundation
import UniformTypeIdentifiers

let extensions = ["md", "markdown", "mdown", "mkd"]

/// Carries a value out of an escaping closure without tripping Swift's rules on
/// capturing a mutable local.
final class ErrorBox: @unchecked Sendable {
    var error: Error?
}

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write(
        Data("usage: set-default-handler.swift <path-to-app-bundle>\n".utf8))
    exit(2)
}

let appURL = URL(fileURLWithPath: CommandLine.arguments[1])
guard let bundleID = Bundle(url: appURL)?.bundleIdentifier else {
    FileHandle.standardError.write(
        Data("error: no bundle identifier at \(appURL.path)\n".utf8))
    exit(1)
}

var failures = 0
var handled = Set<String>()

for ext in extensions {
    guard let type = UTType(filenameExtension: ext) else {
        print("  skipped  .\(ext) (no content type)")
        continue
    }
    guard handled.insert(type.identifier).inserted else { continue }

    // A "dyn.…" identifier is a placeholder macOS invents for an extension no
    // installed app declares. Nothing resolves through it, so binding a
    // handler to one accomplishes nothing.
    if type.identifier.hasPrefix("dyn.") {
        print("  skipped  .\(ext) (undeclared type)")
        continue
    }

    if #available(macOS 14.0, *) {
        // The completion-handler form, explicitly. The async form resolves to
        // an overload that neither throws nor awaits, which swallows the error
        // silently - worth avoiding, since a silent failure is exactly what
        // this code has to detect.
        let box = ErrorBox()
        let semaphore = DispatchSemaphore(value: 0)
        print("  asking   macOS to make \(bundleID) the handler for .\(ext)")
        print("           (confirm the dialog if one appears)")
        NSWorkspace.shared.setDefaultApplication(at: appURL, toOpen: type) { error in
            box.error = error
            semaphore.signal()
        }
        // Generous timeout: macOS treats this as a user decision and may put up
        // a confirmation dialog, which the completion handler waits on. Without
        // a GUI session - a locked screen, an SSH session - it never returns.
        if semaphore.wait(timeout: .now() + 30) == .timedOut {
            print("  FAILED   \(type.identifier): no answer from the system")
            failures += 1
        } else if let error = box.error {
            print("  FAILED   \(type.identifier): \(error.localizedDescription)")
            failures += 1
        } else {
            print("  set      \(type.identifier)")
        }
    } else {
        let status = LSSetDefaultRoleHandlerForContentType(
            type.identifier as CFString, .all, bundleID as CFString)
        if status == noErr {
            print("  set      \(type.identifier) (legacy API)")
        } else {
            print("  FAILED   \(type.identifier) (OSStatus \(status))")
            failures += 1
        }
    }
}

// Report what the system actually resolves now, rather than trusting the calls
// above. This is the check that caught the silent legacy-API failure.
if let markdown = UTType(filenameExtension: "md"),
    let current = LSCopyDefaultRoleHandlerForContentType(
        markdown.identifier as CFString, .all)?.takeRetainedValue() as String?
{
    if current == bundleID {
        print("  verified default handler for .md is \(current)")
    } else {
        print("  WARNING  default handler for .md is \(current), not \(bundleID)")
        failures += 1
    }
}

exit(failures == 0 ? 0 : 1)
