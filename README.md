AI Drama Studio 

video transfer video, mainly for short video not live-streaming.

Any questions, asked me. Telegram:@crazy4OwO

An AI-powered video transformation and localization workspace.

AI Drama Studio (V2V) is a web-based workflow for transforming existing video footage using reference-driven visual generation, shot-level controls, and multilingual post-production tools. It brings visual transformation, review, and localization into a single project-oriented workspace.
Project status: Active development. Features and provider integrations may change. This repository is not currently presented as a turnkey, self-hosted release.

Overview
The platform is designed for creators and production teams who want to adapt existing footage without rebuilding the entire production pipeline. Users can upload a source video, provide visual references, generate transformed footage, review results at the shot or unit level, and continue into transcription, translation, dubbing, subtitles, and final delivery.
Core capabilities
1. Reference-driven video transformation
- Character replacement: Transform on-screen characters using supplied reference images.
- Clothing replacement: Guide wardrobe changes with visual references.
- Background replacement: Adapt the visual environment of existing footage.
- Prompt control: Provide task-specific instructions for supported transformation modes.
- Product-level replacement: Planned; not presented as a completed feature.
2. Two visual workflows
Direct transformation

A streamlined path for eligible videos up to 60 seconds, designed to reduce the need for manual shot planning.

Advanced shot workflow

A reviewable sequence of Shot Plan → Transform Units → Character Mapping → Shot Transformation. Users can inspect individual units, regenerate unsatisfactory results, retain original footage where appropriate, and assemble a visual final output from their selections.

The 60-second threshold describes the current product workflow, not a promise about generation speed or provider availability.
3. Video localization
- Speech transcription with a local Whisper-based pipeline and speaker diarization support.
- Translation and editable timeline-level text.
- Speaker and voice configuration for text-to-speech.
- Subtitle preparation, audio mixing, and final output assembly.
Availability and quality depend on the configured services, source media, and processing environment.
4. Project workspace and delivery
- Project and episode organization.
- Character reference management and mapping.
- Unit-level output review and selection.
- Visual final and localized final delivery workflows.
- Task status, failure diagnostics, and credit-aware generation operations.
Workflow
Source Video + Reference Images
                |
                v
       Visual Transformation
          /            \
         v              v
   Direct Path      Advanced Shot Plan
    (<= 60 s)       + Transform Units
         \              /
          v            v
       Review and Selection
                |
                v
        Visual Final Output
                |
                v
     Transcription / Translation
                |
                v
       Dubbing / Subtitles
                |
                v
          Final Delivery
Technology
The project uses a React / TypeScript frontend and a Python backend, with FFmpeg / FFprobe for media processing. Its workflows integrate external video-generation services and configurable speech, translation, and text-to-speech components. The precise providers, models, and deployment configuration can vary by environment.
Current development focus
- Reliable browser playback and preview compatibility for uploaded media.
- Clear separation between source media and browser-friendly preview assets.
- Robust shot-level generation, review, and output assembly.
- Better task-state handling and actionable error messages.
- Simplified user-facing localization workflows.
Screenshots and demo
Screenshots and before/after samples can be added here once approved for public sharing.
<!-- Suggested structure:
assets/screenshots/workspace.png
assets/screenshots/shot-workbench.png
assets/demo/before-after.mp4
-->Repository and deployment notes
This repository represents a development codebase. A working deployment may require external service credentials, media-processing dependencies, persistent storage, background services, and environment-specific configuration.
