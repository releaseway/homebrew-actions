#!/usr/bin/env ruby
require "open3"
require "rubygems"

def reject(message)
  abort "#{message}. Use allow-downgrade=true for an intentional replacement."
end

def integer_literal(content, key)
  declarations = content.scan(/^\s*#{Regexp.escape(key)}\b([^\n]*)$/).flatten
  return 0 if declarations.empty?
  unless declarations.one? && declarations.first.match?(/\A\s+(?:0|[1-9]\d*)\s*(?:#.*)?\z/)
    abort "Formula #{key} must be a non-negative integer literal"
  end
  declarations.first.strip.to_i
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
new_scheme = integer_literal(candidate, "version_scheme")
new_revision = integer_literal(candidate, "revision")
if baseline == "--package-version"
  puts new_revision.positive? ? "#{version}_#{new_revision}" : version
  exit 0
end

# Read text, never evaluate the existing Formula's Ruby code.
names, error, status = Open3.capture3("git", "ls-tree", "--name-only", baseline, "--", path)
abort error unless status.success?
exit 0 if names.empty?
previous, error, status = Open3.capture3("git", "show", "#{baseline}:#{path}")
abort error unless status.success?
exit 0 if override == "true"
old_scheme = integer_literal(previous, "version_scheme")
old_revision = integer_literal(previous, "revision")

old_version = previous[/^# releaseway-version: (.+)$/, 1] ||
  previous[/^\s*version ["']([^"']+)["']/, 1] ||
  previous[%r{/releases/download/v?([^/]+)/}, 1]
old_commit = previous[/^# releaseway-source-commit: ([0-9a-f]{40})$/, 1] ||
  previous[%r{/archive/([0-9a-f]{40})\.tar\.gz}, 1]

old_release = "(#{old_scheme}, #{old_version}, #{old_revision})"
new_release = "(#{new_scheme}, #{version}, #{new_revision})"
reject "Refusing Formula downgrade from #{old_release} to #{new_release}" if new_scheme < old_scheme
exit 0 if new_scheme > old_scheme

def comparable(value)
  return nil unless value && value.match?(/\A\d+(?:\.\d+)*(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?(?:\+[0-9A-Za-z.-]+)?\z/)
  Gem::Version.new(value.split("+", 2).first)
end

old = comparable(old_version)
new = comparable(version)
reject "Cannot compare existing version #{old_version.inspect} with #{version.inspect}" unless old && new
reject "Refusing Formula downgrade from #{old_release} to #{new_release}" if new < old
if new == old && old_commit != commit
  reject "Same-version Formula updates require the same source commit"
end
if new == old && new_revision < old_revision
  reject "Refusing Formula revision downgrade from #{old_release} to #{new_release}"
end
