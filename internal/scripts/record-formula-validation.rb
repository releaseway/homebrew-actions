#!/usr/bin/env ruby
require "json"
require "open3"
require "time"
require "tempfile"
require "digest"

def require_valid(value, message)
  abort message unless value
end

def command(*args)
  output, _, status = Open3.capture3({ "HOMEBREW_NO_AUTO_UPDATE" => "1" }, *args)
  require_valid(status.success?, "Native runtime lookup failed: #{args.first}")
  output.strip
end

def architecture(value)
  case value.downcase
  when "arm64", "aarch64" then "arm64"
  when "x86_64", "amd64", "x64" then "x86_64"
  else abort "Unknown native architecture"
  end
end

def atomic_json(path, value)
  Tempfile.create([".homebrew-receipt-", ".json"], File.dirname(path)) do |file|
    file.write(JSON.pretty_generate(value) + "\n")
    file.flush
    File.rename(file.path, path)
  end
end

context = JSON.parse(ENV.fetch("FORMULA_CONTEXT"))
platform = ENV.fetch("PLATFORM")
require_valid(context.fetch("required_platforms").include?(platform), "Undeclared native validation target")
runner = context.fetch("runners").fetch(platform)
require_valid(ENV.fetch("REQUESTED_RUNNER") == runner, "Requested runner mismatch")
scope = {
  "invocation_id" => ENV.fetch("INVOCATION_ID"),
  "automation_repository" => ENV.fetch("AUTOMATION_REPOSITORY"),
  "automation_sha" => ENV.fetch("AUTOMATION_SHA"),
  "caller_repository" => ENV.fetch("GITHUB_REPOSITORY"),
  "caller_sha" => ENV.fetch("GITHUB_SHA"),
  "run_id" => Integer(ENV.fetch("GITHUB_RUN_ID"), 10),
  "run_attempt" => Integer(ENV.fetch("GITHUB_RUN_ATTEMPT"), 10),
}
require_valid(scope["invocation_id"].match?(/\A[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\z/), "Invalid invocation UUID")
%w[automation_sha caller_sha].each { |key| require_valid(scope[key].match?(/\A[0-9a-f]{40}\z/), "Invalid workflow commit") }
require_valid(scope["run_id"].positive? && scope["run_attempt"].positive?, "Invalid workflow attempt")
formula_bytes = File.binread(ENV.fetch("FORMULA_PATH"))
require_valid(Digest::SHA256.hexdigest(formula_bytes) == context.fetch("formula_sha256"), "Native Formula digest mismatch")
os = command("uname", "-s")
uname_arch = architecture(command("uname", "-m"))
physical_arch = if os == "Darwin"
  value = command("sysctl", "-n", "hw.optional.arm64")
  require_valid(%w[0 1].include?(value), "Unknown macOS hardware architecture")
  value == "1" ? "arm64" : "x86_64"
else
  uname_arch
end
expected_os, expected_arch = platform.split("-", 2)
require_valid(os == { "macos" => "Darwin", "linux" => "Linux" }.fetch(expected_os), "Native OS mismatch")
require_valid(uname_arch == expected_arch && physical_arch == expected_arch, "Native hardware architecture mismatch")
require_valid(ENV.fetch("RUNNER_OS").downcase == (expected_os == "macos" ? "macos" : "linux") &&
              architecture(ENV.fetch("RUNNER_ARCH")) == expected_arch, "Runner identity mismatch")
brew = JSON.parse(command("brew", "ruby", "-rjson", "-rrbconfig", "-e",
                          'puts JSON.generate(host_cpu: RbConfig::CONFIG.fetch("host_cpu"), ruby_platform: RUBY_PLATFORM)'))
require_valid(architecture(brew.fetch("host_cpu")) == expected_arch && !brew.fetch("ruby_platform").empty?,
              "Homebrew process architecture mismatch")
runtime = {
  "os" => os, "uname_arch" => uname_arch, "hardware_arch" => physical_arch,
  "brew_host_cpu" => brew.fetch("host_cpu"), "ruby_platform" => brew.fetch("ruby_platform"),
  "brew_version" => command("brew", "--version"),
  "runner_os" => ENV.fetch("RUNNER_OS"), "runner_arch" => ENV.fetch("RUNNER_ARCH"),
  "image_os" => ENV["ImageOS"].to_s.empty? ? "unknown" : ENV["ImageOS"],
  "image_version" => ENV["ImageVersion"].to_s.empty? ? "unknown" : ENV["ImageVersion"],
}
require_valid(runtime["brew_version"].start_with?("Homebrew "), "Homebrew version missing")
snapshot = { "scope" => scope, "platform" => platform, "runner" => runner, "runtime" => runtime }
runtime_path = ENV.fetch("NATIVE_RUNTIME")
case ARGV.fetch(0)
when "--preflight"
  atomic_json(runtime_path, snapshot)
when "--record"
  require_valid(ENV.fetch("VALIDATION_SUCCEEDED", "") == "true", "Native audit/install/test did not all succeed")
  require_valid(JSON.parse(File.read(runtime_path)) == snapshot, "Native runtime changed after preflight")
  receipt = context.merge(scope).merge(
    "schema" => 1, "kind" => "homebrew-native", "platform" => platform, "requested_runner" => runner,
    "runtime" => runtime, "checks" => { "audit" => "success", "install" => "success", "test" => "success" },
    "completed_at" => Time.now.utc.iso8601,
  )
  atomic_json(ENV.fetch("NATIVE_RECEIPT"), receipt)
else
  abort "Expected --preflight or --record"
end
puts "Verified native runtime for #{platform}"
