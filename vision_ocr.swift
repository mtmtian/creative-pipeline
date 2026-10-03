// vision_ocr.swift：macOS 自带的 Apple Vision 文字识别，供 analyze.ocr_images 调用（首次使用时自动编译）。
//
//     vision_ocr [--lang ja] image...
//
// 每张图输出一行 JSON：{"file", "lines": [{"text", "conf"(0–100), "left", "top", "width", "height"}]}，
// 坐标是像素、原点在左上，和 tesseract TSV 的口径一致。--lang 取语言代码（en/ja/es…），自动对应到
// Vision 支持的识别语言；Vision 实际只按第一个语言识别，日文素材必须传 ja，否则日文字一个都认不出。
import AppKit
import Foundation
import Vision

var language = "en"
var files: [String] = []
var args = CommandLine.arguments.dropFirst().makeIterator()
while let arg = args.next() {
    if arg == "--lang" {
        language = args.next() ?? language
    } else {
        files.append(arg)
    }
}

let probe = VNRecognizeTextRequest()
probe.recognitionLevel = .accurate
let supported = (try? probe.supportedRecognitionLanguages()) ?? ["en-US"]
let primary = supported.first { $0 == language || $0.hasPrefix(language + "-") } ?? "en-US"
let languages = primary == "en-US" ? [primary] : [primary, "en-US"]

for file in files {
    var lines: [[String: Any]] = []
    if let image = NSImage(contentsOfFile: file),
       let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.recognitionLanguages = languages
        request.usesLanguageCorrection = true
        try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([request])
        let w = Double(cg.width), h = Double(cg.height)
        for observation in request.results ?? [] {
            guard let top = observation.topCandidates(1).first else { continue }
            let box = observation.boundingBox  // 归一化坐标，原点在左下
            lines.append([
                "text": top.string, "conf": Double(top.confidence) * 100,
                "left": Int(box.minX * w), "top": Int((1 - box.maxY) * h),
                "width": Int(box.width * w), "height": Int(box.height * h),
            ])
        }
    }
    let data = try! JSONSerialization.data(withJSONObject: ["file": file, "lines": lines])
    print(String(data: data, encoding: .utf8)!)
}
