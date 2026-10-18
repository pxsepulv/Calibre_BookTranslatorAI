from calibre.customize import InterfaceActionBase

class BookTranslatorAIPlugin(InterfaceActionBase):
    name                    = 'Book Translator AI'
    description             = 'Traducción literaria y técnica de EPUBs mediante IA con Guías de Traducción, glosarios y continuidad de sagas'
    supported_platforms     = ['windows', 'osx', 'linux']
    author                  = 'Pablo'
    version                 = (1, 0, 0)
    minimum_calibre_version = (6, 0, 0)

    actual_plugin           = 'calibre_plugins.book_translator_ai.ui:BookTranslatorAIAction'

    def is_customizable(self):
        return False