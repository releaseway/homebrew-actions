# One declaration drives asset selection, requirements and native validation.
module FormulaPlatforms
  RUNNERS = {
    "macos-arm64" => "macos-latest",
    "macos-x86_64" => "macos-15-intel",
    "linux-arm64" => "ubuntu-24.04-arm",
    "linux-x86_64" => "ubuntu-24.04",
  }.freeze

  def self.normalize(spec)
    explicit = spec.key?("platforms")
    declared = spec["platforms"]
    if explicit
      unless declared.is_a?(Array) && !declared.empty? && declared.all? { |item| RUNNERS.key?(item) }
        raise ArgumentError, "platforms must be a non-empty list of supported platform IDs"
      end
      raise ArgumentError, "platforms contains duplicate platform IDs" unless declared.uniq == declared
      declared = RUNNERS.keys & declared
    end
    distribution = spec["distribution"]
    type = distribution.is_a?(Hash) ? distribution["type"] : "source"
    targets = declared || ["macos-arm64"]
    if type == "github-release"
      assets = distribution["assets"]
      raise ArgumentError, "distribution.assets must be a mapping" unless assets.is_a?(Hash)
      unknown = assets.keys - RUNNERS.keys
      raise ArgumentError, "distribution.assets has unsupported platforms: #{unknown.join(', ')}" unless unknown.empty?
      if explicit
        missing = declared - assets.keys
        extra = assets.keys - declared
        raise ArgumentError, "distribution.assets is missing declared platform mappings: #{missing.join(', ')}" unless missing.empty?
        raise ArgumentError, "distribution.assets has undeclared platform mappings: #{extra.join(', ')}" unless extra.empty?
      else
        systems = %w[macos linux].select { |os| assets.keys.any? { |key| key.start_with?(os + '-') } }
        raise ArgumentError, "distribution.assets must declare at least one supported operating system" if systems.empty?
        systems.each do |os|
          missing = RUNNERS.keys.select { |key| key.start_with?(os + '-') } - assets.keys
          raise ArgumentError, "distribution.assets is missing #{os} platforms: #{missing.join(', ')}" unless missing.empty?
        end
        targets = RUNNERS.keys & assets.keys
      end
    end
    { "mode" => explicit ? "explicit" : "legacy", "declared" => explicit ? declared : nil,
      "targets" => targets, "systems" => %w[macos linux].select { |os| targets.any? { |key| key.start_with?(os + '-') } },
      "runner-matrix" => targets.map { |key| { "platform" => key, "runner" => RUNNERS.fetch(key) } } }
  end

  def self.arch_requirement(model, os)
    return nil unless model.fetch("mode") == "explicit"
    targets = model.fetch("targets").select { |key| key.start_with?(os + '-') }
    return nil unless targets.one?
    targets.first.end_with?("-arm64") ? "arm64" : "x86_64"
  end
end
