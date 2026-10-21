# Parsing/requirement inspection under simulation; never native install evidence.
require "json"
require "pathname"
require "simulate_system"

manifest = JSON.parse(File.read(ARGV.fetch(0)))
hosts = %w[macos-arm64 macos-x86_64 linux-arm64 linux-x86_64]
checked = 0
manifest.each do |entry|
  hosts.each do |host|
    os, arch = host.split("-", 2)
    Homebrew::SimulateSystem.with(os: os == "macos" ? :macos : :linux, arch: arch == "arm64" ? :arm : :intel) do
      formula = Formulary.from_contents(entry.fetch("name"), Pathname.new(entry.fetch("path")), File.read(entry.fetch("path")),
                                       tap: Tap.fetch("releaseway/platform-contracts"))
      sources = entry.fetch("sources")
      raise "Version mismatch: #{entry["name"]}/#{host}" unless formula.version.to_s == "1.2.3"
      expected = if entry.fetch("distribution") == "source"
        sources.fetch("source")
      else
        sources.fetch(host, sources.fetch(entry.fetch("targets").first))
      end
      raise "URL mismatch: #{entry["name"]}/#{host}: #{formula.stable.url}" unless formula.stable.url == expected.fetch("url")
      raise "SHA mismatch: #{entry["name"]}/#{host}" unless formula.stable.checksum.to_s == expected.fetch("sha256")
      requirements = formula.requirements.to_a
      macos_only = requirements.any? { |r| r.class.name == "MacOSRequirement" }
      linux_only = requirements.any? { |r| r.class.name == "LinuxRequirement" }
      arches = requirements.select { |r| r.class.name == "ArchRequirement" }.map { |r| r.arch.to_s }
      allowed = (!macos_only || os == "macos") && (!linux_only || os == "linux") && (arches.empty? || arches.include?(arch))
      raise "Requirement mismatch: #{entry["name"]}/#{host}: #{requirements.inspect}" unless allowed == entry.fetch("targets").include?(host)
      checked += 1
    end
  end
end
raise "Expected 120 source/prebuilt platform-host contracts" unless checked == 120
puts "Homebrew loaded and checked #{checked} platform/host contracts (simulation only)"
