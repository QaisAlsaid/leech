{
  description = "leech dev shell - FHS runtime libs for the venv (numpy/soundfile/torch/kittenml) + ffmpeg CLI";

  inputs = {
    # same local pin as dorg3, so nixpkgs itself costs 0 download
    nixpkgs.url = "path:/nix/store/7blcfay1hap81n2bc9j4d8b1cxrvng50-source";
  };

  outputs =
    { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };

      # the shared libs pip wheels expect from a "normal" linux:
      #   libstdc++.so.6, libgomp.so.1   <- stdenv.cc.cc.lib   (your numpy error)
      #   libgcc_s.so.1                  <- libgcc (own package on newer pins,
      #                                      still inside gcc.cc.lib on old ones)
      #   libz.so.1                      <- zlib (torch links it)
      runtimeLibs =
        with pkgs;
        [
          stdenv.cc.cc.lib
          zlib
        ]
        ++ pkgs.lib.optionals (builtins.hasAttr "libgcc" pkgs) [ pkgs.libgcc ];
    in
    {
      devShells.${system} = {
        default = pkgs.mkShell {
          # ffmpeg as a CLI for any later wav -> ogg-opus / trim work.
          # this is the only real download in the whole shell;
          # delete the line if you don't want it, numpy/torch don't need it.
          nativeBuildInputs = [ pkgs.ffmpeg ];

          shellHook = ''
            export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath runtimeLibs}:$LD_LIBRARY_PATH"

            # put the venv on PATH (survives the exec below, unlike `activate`)
            if [ -d "$PWD/.venv/bin" ]; then
              export PATH="$PWD/.venv/bin:$PATH"
              export VIRTUAL_ENV="$PWD/.venv"
            fi

            exec zsh
          '';
        };
      };
    };
}
