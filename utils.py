def is_admin(user):
    return any(role.name == "Game Admin" for role in user.roles)

async def reject_if_not_admin(interaction):
    if interaction.guild is None or not is_admin(interaction.user):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return True
    return False
