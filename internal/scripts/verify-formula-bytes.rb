#!/usr/bin/env ruby
require "digest"
require "open3"

path = ENV.fetch("FORMULA_PATH")
expected = ENV["FORMULA_SHA256"]
abort "FORMULA_SHA256 is required from the validated artifact" unless expected
abort "FORMULA_SHA256 must be a lowercase SHA-256" unless expected.match?(/\A[0-9a-f]{64}\z/)
abort "Formula must be a regular file" unless File.file?(path) && !File.symlink?(path)
reject = -> { abort "Formula differs from the validated artifact; regenerate and revalidate against the current tap before retrying" }
reject.call unless Digest::SHA256.file(path).hexdigest == expected
if ARGV.include?("--head")
  bytes, error, status = Open3.capture3("git", "show", "HEAD:#{path}")
  abort error unless status.success?
  reject.call unless Digest::SHA256.hexdigest(bytes) == expected
end
