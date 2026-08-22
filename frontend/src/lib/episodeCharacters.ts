import type { Character, Episode } from '../api'

function candidateCharacterId(candidate: NonNullable<Episode['candidates']>[number]) {
  return candidate.matched_character_id || candidate.character_id || ''
}

export function episodeBoundCharacterIds(episode?: Episode): string[] {
  if (!episode) return []
  const ids = new Set<string>()
  for (const match of episode.matches || []) {
    if (match.character_id && match.match_status !== 'rejected') ids.add(match.character_id)
  }
  for (const candidate of episode.candidates || []) {
    const characterId = candidateCharacterId(candidate)
    if (characterId && !['ignored', 'background'].includes(candidate.status)) ids.add(characterId)
  }
  return [...ids]
}

export function episodeBoundCharacters(characters: Character[], episode?: Episode): Character[] {
  const ids = new Set(episodeBoundCharacterIds(episode))
  return characters
    .filter((character) =>
      ids.has(character.id)
      || Boolean(episode && character.created_from_episode_id === episode.id),
    )
    .sort((left, right) =>
      (left.character_code || left.name || left.id).localeCompare(
        right.character_code || right.name || right.id,
      ),
    )
}

export function validEpisodeTargetCharacters(characters: Character[], episode?: Episode): Character[] {
  return episodeBoundCharacters(characters, episode).filter((character) =>
    character.confirmed
    && character.priority_level === 'Locked'
    && Boolean(
      character.target_reference_path
      || character.main_reference_image
      || character.source_reference_image
      || character.original_reference_image
      || character.reference_assets?.some((asset) => asset.is_primary && asset.image),
    ),
  )
}
