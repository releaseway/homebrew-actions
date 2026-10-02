#!/usr/bin/env ruby
require "open3"
require "rubygems"

def reject(message)
  abort "#{message}. Use allow-downgrade=true for an intentional replacement."
end

path = ENV.fetch("FORMULA_PATH")
baseline = ARGV.fetch(0, "HEAD")
override = ENV.fetch("ALLOW_DOWNGRADE", "false")
abort "ALLOW_DOWNGRADE must be true or false" unless %w[true false].include?(override)

candidate = File.read(path)
version = candidate[/^# releaseway-version: (.+)$/, 1]
commit = candidate[/^# releaseway-source-commit: ([0-9a-f]{40})$/, 1]
abort "Generated Formula is missing Releaseway provenance" unless version && commit
abort "Generated Formula version does not match VERSION" unless version == ENV.fetch("VERSION")
abort "Generated Formula commit does not match COMMIT" unless commit == ENV.fetch("COMMIT").downcase

# Read text, never evaluate the existing Formula's Ruby code.
names, error, status = Open3.capture3("git", "ls-tree", "--name-only", baseline, "--", path)
abort error unless status.success?
exit 0 if names.empty?
previous, error, status = Open3.capture3("git", "show", "#{baseline}:#{path}")
abort error unless status.success?
exit 0 if override == "true"

old_version = previous[/^# releaseway-version: (.+)$/, 1] ||
  previous[/^\s*version ["']([^"']+)["']/, 1] ||
  previous[%r{/releases/download/v?([^/]+)/}, 1]
old_commit = previous[/^# releaseway-source-commit: ([0-9a-f]{40})$/, 1] ||
  previous[%r{/archive/([0-9a-f]{40})\.tar\.gz}, 1]

def comparable(value)
  return nil unless value && value.match?(/\A\d+(?:\.\d+)*(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?(?:\+[0-9A-Za-z.-]+)?\z/)
  Gem::Version.new(value.split("+", 2).first)
end

old = comparable(old_version)
new = comparable(version)
reject "Cannot compare existing version #{old_version.inspect} with #{version.inspect}" unless old && new
reject "Refusing Formula downgrade from #{old_version} to #{version}" if new < old
if new == old && old_commit != commit
  reject "Same-version Formula updates require the same source commit"
end
