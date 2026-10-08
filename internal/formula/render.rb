require "digest"
require "fileutils"
require "json"
require "open3"
require "tempfile"
require "yaml"

def fail_with(message)
  warn message
  exit 1
end

def formula_class(name)
  class_name = name.capitalize
  class_name.gsub!(/[-_.\s]([a-zA-Z0-9])/) { Regexp.last_match(1).upcase }
  class_name.tr!("+", "x")
  class_name.sub!(/(.)@(\d)/, "\\1AT\\2")
  class_name
end

def required_string(spec, key)
  value = spec[key]
  fail_with("#{key} is required") unless value.is_a?(String) && !value.empty?
  value
end

def optional_string(spec, key)
  value = spec[key]
  return nil if value.nil?
  fail_with("#{key} must be a string") unless value.is_a?(String)
  value
end

def download_sha256(url)
  Tempfile.create("homebrew-formula-download") do |file|
    file.close
    _, stderr, status = Open3.capture3("curl", "-fsSL", url, "-o", file.path)
    fail_with("failed to download #{url}: #{stderr.strip}") unless status.success?
    Digest::SHA256.file(file.path).hexdigest
  end
end

def github_json(path)
  stdout, stderr, status = Open3.capture3("gh", "api", "--method", "GET", path)
  fail_with("GitHub API request failed for #{path}: #{stderr.strip}") unless status.success?
  JSON.parse(stdout)
rescue JSON::ParserError => e
  fail_with("GitHub API returned invalid JSON for #{path}: #{e.message}")
end

def expand_release_template(value, path, version)
  fail_with("#{path} must be a string") unless value.is_a?(String) && !value.empty?
  expanded = value.gsub("{version}", version)
  fail_with("#{path} contains an unsupported template placeholder") if expanded.include?("{") || expanded.include?("}")
  expanded
end

def safe_release_segment(value, path)
  unless value.match?(/\A[A-Za-z0-9][A-Za-z0-9._+@-]*\z/)
    fail_with("#{path} may contain only letters, numbers, dot, underscore, plus, at sign, and dash")
  end
  value
end

def resolve_distribution(spec, repository, version, source_commit, validation_mode)
  distribution = spec["distribution"]
  if distribution.nil?
    url = "https://github.com/#{repository}/archive/#{source_commit}.tar.gz"
    checksum = validation_mode == "spec" ? "0" * 64 : download_sha256(url)
    return {
      "type" => "source",
      "sources" => { "source" => { "url" => url, "sha256" => checksum } },
    }
  end

  fail_with("distribution must be a mapping") unless distribution.is_a?(Hash)
  type = required_string(distribution, "type")

  case type
  when "source"
    unknown = distribution.keys - %w[type]
    fail_with("unsupported distribution keys for source: #{unknown.join(", ")}") unless unknown.empty?
    url = "https://github.com/#{repository}/archive/#{source_commit}.tar.gz"
    checksum = validation_mode == "spec" ? "0" * 64 : download_sha256(url)
    {
      "type" => "source",
      "sources" => { "source" => { "url" => url, "sha256" => checksum } },
    }
  when "github-release"
    unknown = distribution.keys - %w[type tag assets]
    fail_with("unsupported distribution keys for github-release: #{unknown.join(", ")}") unless unknown.empty?
    tag = safe_release_segment(
      expand_release_template(required_string(distribution, "tag"), "distribution.tag", version),
      "distribution.tag",
    )
    assets = distribution["assets"]
    fail_with("distribution.assets must be a mapping") unless assets.is_a?(Hash)
    platforms_by_system = {
      "macos" => %w[macos-arm64 macos-x86_64],
      "linux" => %w[linux-arm64 linux-x86_64],
    }
    platforms = platforms_by_system.values.flatten
    unknown_platforms = assets.keys - platforms
    fail_with("distribution.assets has unsupported platforms: #{unknown_platforms.join(", ")}") unless unknown_platforms.empty?
    systems = platforms_by_system.filter_map do |system, system_platforms|
      system if (system_platforms & assets.keys).any?
    end
    fail_with("distribution.assets must declare at least one supported operating system") if systems.empty?
    systems.each do |system|
      missing = platforms_by_system.fetch(system) - assets.keys
      fail_with("distribution.assets is missing #{system} platforms: #{missing.join(", ")}") unless missing.empty?
    end

    selected_platforms = systems.flat_map { |system| platforms_by_system.fetch(system) }
    expanded_assets = selected_platforms.to_h do |platform|
      asset = safe_release_segment(
        expand_release_template(assets.fetch(platform), "distribution.assets.#{platform}", version),
        "distribution.assets.#{platform}",
      )
      [platform, asset]
    end

    if validation_mode == "spec"
      sources = expanded_assets.to_h do |platform, asset|
        url = "https://github.com/#{repository}/releases/download/#{tag}/#{asset}"
        [platform, { "url" => url, "sha256" => "0" * 64 }]
      end
      return { "type" => type, "tag" => tag, "systems" => systems, "sources" => sources }
    end

    release_path = "repos/#{repository}/releases/tags/#{tag}"
    release = github_json(release_path)
    fail_with("GitHub Release #{tag} is a draft") if release["draft"]
    fail_with("GitHub Release #{tag} is not published") if release["published_at"].nil?
    fail_with("GitHub Release #{tag} must be immutable") unless release["immutable"] == true

    tag_commit = github_json("repos/#{repository}/commits/#{tag}")["sha"]
    unless tag_commit.is_a?(String) && tag_commit.casecmp?(source_commit)
      fail_with("GitHub Release tag #{tag} does not resolve to source commit #{source_commit}")
    end

    release_assets = release["assets"]
    fail_with("GitHub Release #{tag} assets are unavailable") unless release_assets.is_a?(Array)
    assets_by_name = release_assets.group_by { |asset| asset["name"] }
    sources = expanded_assets.to_h do |platform, asset_name|
      matches = assets_by_name.fetch(asset_name, [])
      fail_with("GitHub Release #{tag} is missing asset #{asset_name}") if matches.empty?
      fail_with("GitHub Release #{tag} contains duplicate asset #{asset_name}") unless matches.one?
      asset = matches.first
      fail_with("GitHub Release asset #{asset_name} is not uploaded") unless asset["state"] == "uploaded"
      url = "https://github.com/#{repository}/releases/download/#{tag}/#{asset_name}"
      digest = asset["digest"]
      checksum = if digest.is_a?(String) && digest.match?(/\Asha256:[0-9a-fA-F]{64}\z/)
        digest.delete_prefix("sha256:").downcase
      else
        download_sha256(url)
      end
      [platform, { "url" => url, "sha256" => checksum }]
    end
    { "type" => type, "tag" => tag, "systems" => systems, "sources" => sources }
  else
    fail_with("distribution.type must be source or github-release")
  end
end

def indent_snippet(value, spaces)
  value.lines.map { |line| line == "\n" ? line : (" " * spaces) + line }.join
end

def normalize_bin_paths(value)
  value.gsub(/"#\{bin\}\/([^"\s]+)"/, 'bin/"\\1"')
end

def render_snippet_block(name, value)
  return "" if value.nil?
  content = +"  #{name} do\n"
  content << indent_snippet(value, 4)
  content << "\n" unless content.end_with?("\n")
  content << "  end\n"
end

def render_method(name, value)
  return "" if value.nil?
  content = +"  def #{name}\n"
  content << indent_snippet(value, 4)
  content << "\n" unless content.end_with?("\n")
  content << "  end\n"
end

def render_license(value)
  case value
  when String
    return ":cannot_represent" if value == "cannot_represent"
    value.dump
  when Hash
    fail_with("license may contain exactly one key") unless value.length == 1
    key, licenses = value.first
    fail_with("license key must be any_of or all_of") unless %w[any_of all_of].include?(key)
    fail_with("license #{key} must be a non-empty string list") unless licenses.is_a?(Array) && licenses.all? { |license| license.is_a?(String) } && !licenses.empty?
    "#{key}: #{licenses.map(&:dump).join(", ").then { |items| "[#{items}]" }}"
  else
    fail_with("license must be a string or any_of/all_of mapping")
  end
end

def string_list(value, path)
  fail_with("#{path} must be a list of strings") unless value.is_a?(Array) && value.all? { |entry| entry.is_a?(String) }
  value
end

def optional_list(spec, key)
  value = spec[key]
  return [] if value.nil?
  fail_with("#{key} must be a list") unless value.is_a?(Array)
  value
end

def symbol_or_string(value, path, symbolic_values: nil)
  fail_with("#{path} must be a string") unless value.is_a?(String) && !value.empty?
  if value.match?(/\A[a-z][a-z0-9_]*\z/) && (symbolic_values.nil? || symbolic_values.include?(value))
    ":#{value}"
  else
    value.dump
  end
end

def dependency_lines(value)
  return [] if value.nil?
  fail_with("dependencies must be a mapping") unless value.is_a?(Hash)

  allowed = %w[runtime build test recommended optional]
  unknown = value.keys - allowed
  fail_with("unsupported dependency groups: #{unknown.join(", ")}") unless unknown.empty?

  entries = []
  string_list(value["runtime"] || [], "dependencies.runtime").each do |name|
    entries << [2, name, "  depends_on #{name.dump}"]
  end

  {
    "build" => [0, ":build"],
    "test" => [1, ":test"],
    "recommended" => [3, ":recommended"],
    "optional" => [4, ":optional"],
  }.each do |group, (order, qualifier)|
    string_list(value[group] || [], "dependencies.#{group}").each do |name|
      entries << [order, name, "  depends_on #{name.dump} => #{qualifier}"]
    end
  end
  duplicates = entries.group_by { |_, name, _| name.downcase }.select { |_, grouped| grouped.length > 1 }.keys
  fail_with("dependencies may not contain duplicate names across groups: #{duplicates.join(", ")}") unless duplicates.empty?
  entries.sort_by { |order, name, _| [order, name.downcase] }.map(&:last)
end

def option_lines(value)
  optional_list({ "options" => value }, "options").map.with_index do |entry, index|
    path = "options[#{index}]"
    fail_with("#{path} must be a mapping") unless entry.is_a?(Hash)
    unknown = entry.keys - %w[name description]
    fail_with("unsupported #{path} keys: #{unknown.join(", ")}") unless unknown.empty?
    name = required_string(entry, "name")
    description = required_string(entry, "description")
    "  option #{name.dump}, #{description.dump}"
  end
end

def conflicts_with_lines(value)
  optional_list({ "conflicts_with" => value }, "conflicts_with").map.with_index do |entry, index|
    case entry
    when String
      "  conflicts_with #{entry.dump}"
    when Hash
      unknown = entry.keys - %w[formula because]
      fail_with("unsupported conflicts_with[#{index}] keys: #{unknown.join(", ")}") unless unknown.empty?
      formula = required_string(entry, "formula")
      because = optional_string(entry, "because")
      line = "  conflicts_with #{formula.dump}"
      line << ", because: #{because.dump}" if because
      line
    else
      fail_with("conflicts_with[#{index}] must be a string or mapping")
    end
  end
end

def uses_from_macos_lines(value)
  entries = optional_list({ "uses_from_macos" => value }, "uses_from_macos").map.with_index do |entry, index|
    case entry
    when String
      [2, entry, "  uses_from_macos #{entry.dump}"]
    when Hash
      unknown = entry.keys - %w[name since tags]
      fail_with("unsupported uses_from_macos[#{index}] keys: #{unknown.join(", ")}") unless unknown.empty?
      name = required_string(entry, "name")
      tags = dependency_tags(entry["tags"], "uses_from_macos[#{index}].tags")
      order = dependency_tag_order(tags)
      dependency = tags.empty? ? name.dump : "#{name.dump} => #{render_tags(tags)}"
      line = "  uses_from_macos #{dependency}"
      since = optional_string(entry, "since")
      line << ", since: #{symbol_or_string(since, "uses_from_macos[#{index}].since")}" if since
      [order, name, line]
    else
      fail_with("uses_from_macos[#{index}] must be a string or mapping")
    end
  end
  entries.sort_by { |order, name, _| [order, name.downcase] }.map(&:last)
end

def dependency_tags(value, path)
  return [] if value.nil?
  tags = value.is_a?(Array) ? value : [value]
  allowed = %w[build test recommended optional]
  fail_with("#{path} must be a string or list of strings") unless tags.all? { |tag| tag.is_a?(String) }
  unknown = tags - allowed
  fail_with("unsupported #{path}: #{unknown.join(", ")}") unless unknown.empty?
  tags
end

def dependency_tag_order(tags)
  return 0 if tags.include?("build")
  return 1 if tags.include?("test")
  return 3 if tags.include?("recommended")
  return 4 if tags.include?("optional")
  2
end

def render_tags(tags)
  rendered = tags.map { |tag| ":#{tag}" }
  rendered.length == 1 ? rendered.first : "[#{rendered.join(", ")}]"
end

def link_overwrite_lines(value)
  return [] if value.nil?
  string_list(value, "link_overwrite").map do |path|
    "  link_overwrite #{path.dump}"
  end
end

def lifecycle_line(method, value)
  return nil if value.nil?
  fail_with("#{method} must be a mapping") unless value.is_a?(Hash)
  date = required_string(value, "date")
  because = value.fetch("because") { fail_with("#{method}.because is required") }
  reasons = %w[
    checksum_mismatch deprecated_upstream does_not_build no_license
    repo_archived repo_removed unmaintained unsupported versioned_formula
  ]
  allowed = %w[date because replacement replacement_formula replacement_cask]
  unknown = value.keys - allowed
  fail_with("unsupported #{method} keys: #{unknown.join(", ")}") unless unknown.empty?
  replacements = %w[replacement replacement_formula replacement_cask].select { |key| value.key?(key) }
  fail_with("#{method} may contain only one replacement key") if replacements.length > 1

  parts = ["date: #{date.dump}", "because: #{symbol_or_string(because, "#{method}.because", symbolic_values: reasons)}"]
  replacements.each do |key|
    replacement = required_string(value, key)
    parts << "#{key}: #{replacement.dump}"
  end
  "  #{method}! #{parts.join(", ")}"
end

def keg_only_line(value)
  return nil if value.nil?
  reasons = %w[provided_by_macos shadowed_by_macos versioned_formula]
  "  keg_only #{symbol_or_string(value, "keg_only", symbolic_values: reasons)}"
end

def ensure_known_keys(spec)
  allowed = %w[
    name caveats conflicts_with dependencies deprecate disable desc homepage install keg_only
    license link_overwrite livecheck options post_install service test distribution
    uses_from_macos version_scheme
  ]
  unknown = spec.keys - allowed
  fail_with("unsupported formula spec keys: #{unknown.join(", ")}") unless unknown.empty?
end

source_root = File.realpath(ENV.fetch("SOURCE_PATH"))
spec_path = File.realpath(ENV.fetch("SPEC"))
unless spec_path.start_with?(source_root + File::SEPARATOR)
  fail_with("spec-path must stay inside the source repository")
end

begin
  spec = YAML.safe_load(File.read(spec_path), permitted_classes: [], aliases: false)
rescue Psych::Exception => e
  fail_with("formula spec YAML is invalid: #{e.message}")
end

fail_with("formula spec must be a mapping") unless spec.is_a?(Hash)
ensure_known_keys(spec)
version_scheme = spec.fetch("version_scheme", 0)
unless version_scheme.is_a?(Integer) && version_scheme >= 0
  fail_with("version_scheme must be a non-negative integer")
end

formula = required_string(spec, "name")
unless formula.match?(/\A[A-Za-z0-9._+@-]+\z/)
  fail_with("name may contain only letters, numbers, dot, underscore, plus, at sign, and dash")
end
class_name = formula_class(formula)
fail_with("formula renders an invalid Ruby class name: #{class_name}") unless class_name.match?(/\A[A-Z]\w*\z/)
desc = required_string(spec, "desc")
homepage = optional_string(spec, "homepage") || "https://github.com/#{ENV.fetch("REPOSITORY")}"
license = spec.fetch("license") { fail_with("license is required") }
install = normalize_bin_paths(required_string(spec, "install"))
test = normalize_bin_paths(required_string(spec, "test"))
distribution = resolve_distribution(
  spec,
  ENV.fetch("REPOSITORY"),
  ENV.fetch("VERSION"),
  ENV.fetch("SOURCE_COMMIT"),
  ENV.fetch("VALIDATION_MODE"),
)
dependencies = dependency_lines(spec["dependencies"])
options = option_lines(spec["options"])
conflicts_with = conflicts_with_lines(spec["conflicts_with"])
uses_from_macos = uses_from_macos_lines(spec["uses_from_macos"])
link_overwrite = link_overwrite_lines(spec["link_overwrite"])
lifecycle = [
  lifecycle_line("deprecate", spec["deprecate"]),
  lifecycle_line("disable", spec["disable"]),
].compact

content = +"# releaseway-version: #{ENV.fetch("VERSION")}\n"
content << "# releaseway-source-commit: #{ENV.fetch("SOURCE_COMMIT")}\n"
content << "class #{class_name} < Formula\n"
content << "  desc #{desc.dump}\n"
content << "  homepage #{homepage.dump}\n"
if distribution["type"] == "source"
  source = distribution.fetch("sources").fetch("source")
  content << "  url #{source.fetch("url").dump}\n"
  content << "  version #{ENV.fetch("VERSION").dump}\n"
  content << "  sha256 #{source.fetch("sha256").dump}\n"
else
  sources = distribution.fetch("sources")
end
content << "  license #{render_license(license)}\n"
content << "  version_scheme #{version_scheme}\n" if version_scheme.positive?
content << "\n"

if distribution["type"] == "github-release"
  {
    "macos" => ["macos-arm64", "macos-x86_64"],
    "linux" => ["linux-arm64", "linux-x86_64"],
  }.each do |os, (arm_platform, intel_platform)|
    next unless distribution.fetch("systems").include?(os)

    arm = sources.fetch(arm_platform)
    intel = sources.fetch(intel_platform)
    content << "  on_#{os} do\n"
    content << "    on_arm do\n"
    content << "      url #{arm.fetch("url").dump}\n"
    content << "      sha256 #{arm.fetch("sha256").dump}\n"
    content << "    end\n"
    content << "    on_intel do\n"
    content << "      url #{intel.fetch("url").dump}\n"
    content << "      sha256 #{intel.fetch("sha256").dump}\n"
    content << "    end\n"
    content << "  end\n\n"
  end
end

class_stanzas = []
if distribution["type"] == "github-release" && distribution.fetch("systems").length == 1
  class_stanzas << "  depends_on :#{distribution.fetch("systems").first}"
end
class_stanzas << keg_only_line(spec["keg_only"])
class_stanzas.concat(options)
class_stanzas.concat(lifecycle)
class_stanzas.concat(dependencies)
class_stanzas.concat(uses_from_macos)
class_stanzas.concat(conflicts_with)
class_stanzas.concat(link_overwrite)
class_stanzas.compact!

livecheck = render_snippet_block("livecheck", optional_string(spec, "livecheck"))
unless livecheck.empty?
  content << livecheck
  content << "\n"
end

unless class_stanzas.empty?
  content << class_stanzas.join("\n")
  content << "\n\n"
end

content << "  def install\n"
content << indent_snippet(install, 4)
content << "\n" unless content.end_with?("\n")
content << "  end\n"
content << "\n"
post_install = render_method("post_install", optional_string(spec, "post_install"))
unless post_install.empty?
  content << post_install
  content << "\n"
end
caveats = render_method("caveats", optional_string(spec, "caveats"))
unless caveats.empty?
  content << caveats
  content << "\n"
end
service = render_snippet_block("service", optional_string(spec, "service"))
unless service.empty?
  content << service
  content << "\n"
end
content << "  test do\n"
content << indent_snippet(test, 4)
content << "\n" unless content.end_with?("\n")
content << "  end\n"
content << "end\n"

formula_dir = File.join(ENV.fetch("TAP_PATH"), "Formula")
FileUtils.mkdir_p(formula_dir)
formula_path = File.join(formula_dir, "#{formula}.rb")
begin
  RubyVM::InstructionSequence.compile(content)
rescue SyntaxError => e
  fail_with("generated Formula is invalid Ruby: #{e.message}")
end
File.write(formula_path, content)

source = distribution.fetch("sources")["source"]
runner_matrix = if ENV.fetch("VALIDATION_MODE") == "spec"
  [{ "platform" => "spec", "runner" => "ubuntu-latest" }]
elsif distribution["type"] == "source"
  [{ "platform" => "macos-arm64", "runner" => "macos-latest" }]
else
  runners = {
    "macos-arm64" => "macos-latest",
    "macos-x86_64" => "macos-15-intel",
    "linux-arm64" => "ubuntu-24.04-arm",
    "linux-x86_64" => "ubuntu-latest",
  }
  distribution.fetch("systems").flat_map do |system|
    platforms = system == "macos" ? %w[macos-arm64 macos-x86_64] : %w[linux-arm64 linux-x86_64]
    platforms.map { |platform| { "platform" => platform, "runner" => runners.fetch(platform) } }
  end
end
File.open(ENV.fetch("GITHUB_OUTPUT"), "a") do |output|
  output.puts "formula=#{formula}"
  output.puts "formula-path=Formula/#{formula}.rb"
  output.puts "archive-url=#{source&.fetch("url", "") || ""}"
  output.puts "sha256=#{source&.fetch("sha256", "") || ""}"
  output.puts "commit=#{ENV.fetch("SOURCE_COMMIT")}"
  output.puts "version=#{ENV.fetch("VERSION")}"
  output.puts "distribution=#{distribution.fetch("type")}"
  output.puts "release-tag=#{distribution.fetch("tag", "")}"
  output.puts "validation-mode=#{ENV.fetch("VALIDATION_MODE")}"
  output.puts "runner-matrix=#{JSON.generate(runner_matrix)}"
end
puts "updated Formula/#{formula}.rb for #{ENV.fetch("REPOSITORY")}@#{ENV.fetch("SOURCE_COMMIT")}"
